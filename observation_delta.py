"""Lossless-for-observed-fields semantic delta output for browser tool calls.

This is not a visual replacement. The first result is full. Follow-up calls
with the matching snapshot_id return changed DOM elements and text only.
A stale or unknown base produces a full result, so no context is silently lost.
"""
from __future__ import annotations

import hashlib
import json
import time

MAX_WORKSPACES = 64
TTL_SECONDS = 1800


def _snapshot(value: dict) -> dict:
    return {
        "url": value.get("url"),
        "title": value.get("title"),
        "elements": value.get("elements", [])[:60],
        "visible_text": value.get("visible_text", "")[:1500],
    }


def _identity(item: dict, index: int) -> str:
    for field in ("testId", "id"):
        if item.get(field):
            return field + ":" + str(item[field])
    return "position:" + str(index) + ":" + str(item.get("tag", ""))


def _map(elements: list) -> dict:
    # Duplicated IDs (poorly authored websites) need separate identities.
    mapped = {}
    for index, element in enumerate(elements):
        identity = _identity(element, index)
        if identity in mapped:
            identity += ":" + str(index)
        mapped[identity] = element
    return mapped


class ObservationCache:
    def __init__(self):
        self._recent: dict[str, dict] = {}

    def format(self, workspace_id: str, observed: dict, base_id: str | None = None) -> dict:
        if not observed.get("ok"):
            return {"ok": False, "error": observed.get("error", "Observation unavailable")}
        now = time.monotonic()
        for key, record in list(self._recent.items()):
            if now - record["time"] > TTL_SECONDS:
                self._recent.pop(key, None)
        current = _snapshot(observed)
        content = json.dumps(current, sort_keys=True, ensure_ascii=False, default=str)
        snapshot_id = hashlib.sha256(content.encode()).hexdigest()[:20]
        previous = self._recent.get(workspace_id)
        self._recent.pop(workspace_id, None)
        self._recent[workspace_id] = {"id": snapshot_id, "snapshot": current, "time": now}
        while len(self._recent) > MAX_WORKSPACES:
            self._recent.pop(next(iter(self._recent)))

        if previous is None or base_id != previous["id"]:
            return {"ok": True, "format": "full", "snapshot_id": snapshot_id,
                    "snapshot": current}

        old = previous["snapshot"]
        if old == current:
            return {"ok": True, "format": "delta", "base_id": base_id,
                    "snapshot_id": snapshot_id, "changed": False}

        old_map = _map(old["elements"])
        new_map = _map(current["elements"])
        added = {key: element for key, element in new_map.items() if key not in old_map}
        removed = [key for key in old_map if key not in new_map]
        updated = {key: element for key, element in new_map.items()
                   if key in old_map and element != old_map[key]}
        changes = {}
        if current["url"] != old["url"]:
            changes["url"] = current["url"]
        if current["title"] != old["title"]:
            changes["title"] = current["title"]
        if current["visible_text"] != old["visible_text"]:
            original, new = old["visible_text"], current["visible_text"]
            if new.startswith(original):
                changes["text_appended"] = new[len(original):]
            else:
                changes["visible_text"] = new

        return {"ok": True, "format": "delta", "base_id": base_id,
                "snapshot_id": snapshot_id, "changed": True,
                "added": added, "removed": removed, "updated": updated,
                "fields": changes}
