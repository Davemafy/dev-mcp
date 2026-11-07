"""Model-authored browser programs with observation-led repair.

A program is a generated sequence of Playwright interactions, never a static
keyword route. The next model turn can inspect failures and resubmit a revised
program. No arbitrary page JavaScript or host subprocesses are exposed.
"""
from __future__ import annotations

import json
from typing import Any

from stream_browser_server import validate_action

MAX_STEPS = 12
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

    def execute(self, workspace_id: str, steps: list[dict], max_repairs: int = 2):
        # max_repairs is a budget for model-led retries, not a false promise
        # that this tool has an independent LLM inside it.
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
                    "max_repairs": max_repairs,
                }
        observation = self.observe(workspace_id)
        return {"ok": True, "trace": trace,
                "final": observation,
                "verified": bool(observation.get("ok"))}
