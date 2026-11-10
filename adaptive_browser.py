"""Model-authored browser programs with observation-led repair.

A program is a generated sequence of Playwright interactions, never a static
keyword route. The next model turn can inspect failures and resubmit a revised
program. No arbitrary page JavaScript or host subprocesses are exposed.
"""
from __future__ import annotations

import json
import secrets
import re
import time
from typing import Any

from stream_browser_server import validate_action
from observation_delta import ObservationCache

MAX_STEPS = 12
REPAIR_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
REPAIR_TTL_SECONDS = 3600
MAX_REPAIR_SESSIONS = 128
SUPPORTED = {
    "navigate", "back", "forward", "reload", "click", "fill", "press",
    "keypress", "type", "scroll", "click_at", "wait_for", "assert_text",
    "inspect_dom",
}


def validate_program(steps: list[dict], viewport: tuple[int, int]):
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError("A browser program requires 1–12 steps.")
    if len(json.dumps(steps)) > 8000:
        raise ValueError("Browser program is too large.")
    checked = []
    for original in steps:
        if not isinstance(original, dict) or original.get("action") not in SUPPORTED:
            raise ValueError("Unsupported browser program action.")
        checked.append(validate_action(original, viewport))
    return checked


class AdaptiveBrowser:
    def __init__(self, streaming):
        self.streaming = streaming
        # Deliberately scoped to this gateway process. Durable jobs need a
        # persistent queue, not falsely persistent in-memory retry claims.
        self._repairs: dict[tuple[str, str], dict] = {}
        self._observations = ObservationCache()

    def _repair_ticket(self, workspace_id: str, repair_id: str | None,
                       max_repairs: int) -> tuple[str, dict] | None:
        now = time.monotonic()
        for key, lease in list(self._repairs.items()):
            if now - lease["last_seen"] > REPAIR_TTL_SECONDS:
                self._repairs.pop(key, None)
        if repair_id is not None:
            if not REPAIR_ID.fullmatch(repair_id):
                raise ValueError("Invalid repair ID.")
            key = (workspace_id, repair_id)
            lease = self._repairs.get(key)
            if lease is None:
                return None
        else:
            if len(self._repairs) >= MAX_REPAIR_SESSIONS:
                raise RuntimeError("Active repair session limit reached.")
            repair_id = secrets.token_urlsafe(12)
            key = (workspace_id, repair_id)
            lease = {"used": 0, "limit": 1 + max_repairs, "last_seen": now}
            self._repairs[key] = lease
        lease["last_seen"] = now
        return repair_id, lease

    def observe(self, workspace_id: str):
        result = self.streaming.action(workspace_id, action="inspect_dom")
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "Browser observation failed")}
        return {
            "ok": True,
            "url": result.get("url"),
            "title": result.get("title"),
            "elements": result.get("elements", [])[:60],
            "visible_text": result.get("visible_text", "")[:1500],
            "frame_version": result.get("frame_version"),
        }

    def observe_delta(self, workspace_id: str, base_id: str | None = None) -> dict:
        """Return the full DOM once, then deterministic changes against a matching base."""
        return self._observations.format(workspace_id, self.observe(workspace_id), base_id)

    def execute(self, workspace_id: str, steps: list[dict],
                max_repairs: int = 2, repair_id: str | None = None):
        # A model-driven correction must reuse the returned repair_id.
        # The initial action plus max_repairs corrections are permitted.
        # The sandbox browser independently blocks identical no-progress loops.
        if not isinstance(max_repairs, int) or not 0 <= max_repairs <= 3:
            raise ValueError("Retry budget must be between zero and three.")
        state = self.streaming.status(workspace_id)
        size = (
            int(state.get("width", 1440)),
            int(state.get("height", 900)),
        )
        if not state.get("ready"):
            return {"ok": False, "error": "Browser is not ready."}
        program = validate_program(steps, size)
        ticket = self._repair_ticket(workspace_id, repair_id, max_repairs)
        if ticket is None:
            return {"ok": False, "error": "unknown-or-expired-repair-id",
                    "message": "Start a new explicit repair session."}
        repair_id, lease = ticket
        if lease["used"] >= lease["limit"]:
            return {
                "ok": False, "error": "repair-budget-exhausted",
                "repair_id": repair_id, "remaining_repairs": 0,
                "message": "The repair budget is exhausted. Inspect and ask for direction, rather than retrying blindly.",
            }
        lease["used"] += 1
        remaining = max(0, lease["limit"] - lease["used"])
        trace: list[dict[str, Any]] = []
        for index, step in enumerate(program):
            reply = self.streaming.action(workspace_id, **step)
            trace.append({
                "step": index + 1,
                "action": step["action"],
                "ok": bool(reply.get("ok")),
                "url": reply.get("url"),
                "error": reply.get("error"),
                "frame_version": reply.get("frame_version"),
            })
            if not reply.get("ok"):
                observation = self.observe(workspace_id)
                return {
                    "ok": False, "failed_at": index + 1,
                    "reason": reply.get("error", "Browser action failed."),
                    "trace": trace, "observation": observation,
                    "next_action": (
                        "Inspect the returned elements and revise the failing "
                        "selector or step before retrying. Preserve successful steps."
                    ),
                    "repair_id": repair_id,
                    "remaining_repairs": remaining,
                }
        observation = self.observe(workspace_id)
        if observation.get("ok"):
            self._repairs.pop((workspace_id, repair_id), None)
        return {"ok": True, "trace": trace, "final": observation,
                "verified": bool(observation.get("ok")),
                "repair_id": repair_id}
