"""Local, deterministic no-progress detection for a live browser.

No DOM data or credentials are stored by this guard: only SHA-256 digests
of the current page state and action. State lives inside the isolated E2B
browser process and therefore survives MCP tool calls, but not browser restarts.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
from dataclasses import dataclass, field

MAX_NO_PROGRESS = 3
MAX_TRACKED = 64
MUTATING = frozenset({
    "navigate", "back", "forward", "reload", "click", "fill",
    "press", "keypress", "type", "scroll", "click_at",
})


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def page_fingerprint(page) -> str:
    """Observe visible and actionable DOM plus navigation/scroll, without screenshot bytes.

    Reading input values is intentionally excluded. The browser evaluates this
    locally; only a hash leaves the page. A CSS-only animation is not progress.
    """
    try:
        signature = page.evaluate("""() => ({
            url: location.href,
            title: document.title,
            text: (document.body?.innerText || '').slice(0, 8000),
            x: Math.round(window.scrollX),
            y: Math.round(window.scrollY),
            controls: Array.from(document.querySelectorAll(
                'a,button,input,textarea,select,[role="button"]'
            )).slice(0, 80).map(el => ({
                tag: el.tagName, type: el.getAttribute('type'),
                id: el.id, role: el.getAttribute('role'),
                label: el.getAttribute('aria-label'),
                testId: el.getAttribute('data-testid'),
                text: (el.innerText || '').slice(0, 150),
                disabled: !!el.disabled,
                checked: !!el.checked
            }))
        })""")
    except Exception:
        # A navigation/cross-process transition can interrupt JS evaluation;
        # let Playwright's normal error handling determine whether to proceed.
        signature = {"url": getattr(page, "url", "unknown"), "unavailable": True}
    return digest(signature)


@dataclass
class NoProgressCircuit:
    """Blocks the fourth identical mutation at the same DOM fingerprint."""
    seen: OrderedDict[str, int] = field(default_factory=OrderedDict)
    last_reason: str = ""

    def can_run(self, action: dict, before: str) -> bool:
        if action.get("action") not in MUTATING:
            return True
        key = digest({"action": action, "page": before})
        if self.seen.get(key, 0) >= MAX_NO_PROGRESS:
            self.last_reason = "no-progress-loop"
            return False
        return True

    def record(self, action: dict, before: str, after: str):
        if action.get("action") not in MUTATING:
            return
        if before != after:
            self.seen.clear()
            self.last_reason = ""
            return
        key = digest({"action": action, "page": before})
        self.seen[key] = self.seen.get(key, 0) + 1
        self.seen.move_to_end(key)
        while len(self.seen) > MAX_TRACKED:
            self.seen.popitem(last=False)

    def reset(self):
        self.seen.clear()
        self.last_reason = ""
