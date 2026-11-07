"""Regression tests for browser no-progress guard and compact observations."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from progress_guard import NoProgressCircuit, MAX_NO_PROGRESS, page_fingerprint
from observation_delta import ObservationCache
from adaptive_browser import AdaptiveBrowser


def test_identical_action_is_blocked_after_three_no_progress_attempts():
    guard = NoProgressCircuit()
    action = {"action": "click", "selector": "#does-not-change"}
    state = "page-fingerprint-abc"
    for _ in range(MAX_NO_PROGRESS):
        assert guard.can_run(action, state)
        guard.record(action, state, state)
    assert not guard.can_run(action, state)
    assert guard.last_reason == "no-progress-loop"
    assert guard.can_run({"action": "inspect_dom"}, state)
    assert guard.can_run({"action": "click", "selector": "#different"}, state)
    guard.reset()
    assert guard.can_run(action, state)


def test_real_page_change_clears_stale_loop_counters():
    guard = NoProgressCircuit()
    action = {"action": "click", "selector": "button"}
    guard.record(action, "before", "before")
    guard.record(action, "before", "before")
    assert guard.can_run(action, "before")
    guard.record(action, "before", "after")
    assert guard.can_run(action, "before")


def test_guard_bounded_and_never_saves_raw_dom_or_credentials():
    guard = NoProgressCircuit()
    for n in range(100):
        action = {"action": "fill", "selector": "#input", "value": "secret-" + str(n)}
        guard.record(action, "private-fingerprint", "private-fingerprint")
    assert len(guard.seen) <= 64
    assert "secret-" not in repr(guard.seen)


def test_fingerprint_uses_semantics_not_refreshing_frame_counter():
    class Page:
        url = "https://example.com"
        def evaluate(self, script):
            assert "document.body?.innerText" in script
            assert "window.scrollY" in script
            assert "el.value" not in script
            return {"url": self.url, "text": "Hello", "y": 0}

    x = Page()
    first = page_fingerprint(x)
    assert first == page_fingerprint(x)
    x.url = "https://example.com/next"
    assert first != page_fingerprint(x)


def test_server_checks_guard_before_executing_playwright_action():
    source = Path("stream_browser_server.py").read_text()
    assert "state.guard.can_run(payload, before)" in source
    assert "raise NoProgressBlocked()" in source
    assert '\"error\": \"no-progress-loop\"' in source
    assert 'state.guard.record(payload, before, page_fingerprint(page))' in source
    assert '"guard_reset"}' not in source


def test_model_repair_budget_requires_same_id_and_actually_stops_retrying():
    class FakeBrowser:
        def __init__(self):
            self.mutations = 0
        def status(self, workspace_id):
            return {"ready": True, "width": 1000, "height": 1000}
        def action(self, workspace_id, action, **kwargs):
            if action == "inspect_dom":
                return {"ok": True, "elements": [], "visible_text": "Unchanged"}
            self.mutations += 1
            return {"ok": False, "error": "TimeoutError"}

    fake = FakeBrowser()
    runner = AdaptiveBrowser(fake)
    initial = runner.execute("signed", [{"action": "click", "selector": "#old"}], max_repairs=1)
    assert not initial["ok"] and initial["remaining_repairs"] == 1
    repair_id = initial["repair_id"]
    second = runner.execute("signed", [{"action": "click", "selector": "#new"}],
                            max_repairs=3, repair_id=repair_id)
    assert not second["ok"] and second["remaining_repairs"] == 0
    blocked = runner.execute("signed", [{"action": "click", "selector": "#yet-again"}],
                             max_repairs=3, repair_id=repair_id)
    assert blocked["error"] == "repair-budget-exhausted"
    assert fake.mutations == 2
    assert runner.execute("signed", [{"action": "click", "selector": "#bad"}],
                          repair_id="does-not-exist")["error"] == "unknown-or-expired-repair-id"
    assert fake.mutations == 2


def test_semantic_delta_is_compact_and_falls_back_to_full_if_base_unknown():
    cache = ObservationCache()
    elements = [
        {"tag": "button", "id": f"action-{n}", "name": f"Option {n}", "role": "button",
         "testId": f"action-{n}", "label": ""}
        for n in range(30)
    ]
    base = {"ok": True, "url": "https://example.com",
            "title": "Preview", "elements": elements,
            "visible_text": "One repeated paragraph " * 48}
    first = cache.format("workspace", base)
    assert first["format"] == "full" and "snapshot" in first
    unchanged = cache.format("workspace", base, first["snapshot_id"])
    assert unchanged["format"] == "delta" and not unchanged["changed"]
    assert len(json.dumps(unchanged)) < len(json.dumps(first)) // 3

    revised = {**base, "elements": [*elements[:-1],
               {**elements[-1], "name": "Updated label"}],
               "visible_text": base["visible_text"] + "Tail"}
    update = cache.format("workspace", revised, unchanged["snapshot_id"])
    assert update["changed"] and update["format"] == "delta"
    assert len(update["updated"]) == 1
    assert update["fields"]["text_appended"] == "Tail"
    assert len(json.dumps(update)) < len(json.dumps(first)) // 2

    fallback = cache.format("workspace", revised, "unknown-base")
    assert fallback["format"] == "full"


def test_delta_never_crosses_workspace_boundaries():
    cache = ObservationCache()
    snapshot = {"ok": True, "url": "https://safe.example",
                "elements": [], "visible_text": "Hello"}
    first = cache.format("owner-A", snapshot)
    second = cache.format("owner-B", snapshot, first["snapshot_id"])
    assert second["format"] == "full"

def test_tools_expose_repair_ticket_and_delta_without_public_guard_reset():
    source = Path("main.py").read_text()
    assert "def browser_observe_delta(" in source
    assert "repair_id: str | None = None" in source
    assert "adaptive.execute(workspace_id, steps, max_repairs, repair_id)" in source
    server = Path("stream_browser_server.py").read_text()
    assert '"guard_reset"}' not in server
