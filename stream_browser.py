"""Owner-scoped cloud browser streaming controls.

The gateway does not host a browser. All browser data, the token, and the page
remain in a single signed E2B workspace; its externally exposed preview uses
an independent high-entropy access capability.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
import shlex
from pathlib import Path

from browser import validate_browser_input
from stream_browser_server import PORT, validate_action

SESSION_DIR = "/home/user/.devmcp-stream"
CONFIG = SESSION_DIR + "/config.json"
SERVER = SESSION_DIR + "/server.py"
CONTROL = SESSION_DIR + "/control.py"


class BrowserStream:
    def __init__(self, machine):
        self.machine = machine

    def _read_config(self, workspace_id):
        try:
            saved = self.machine.read(workspace_id, CONFIG)
            return json.loads(saved["content"])
        except Exception:
            # Missing configs have SDK-specific exception types. All file
            # requests already pass through the signed workspace gate.
            return None

    def _local(self, workspace_id, payload=None):
        args = "--status" if payload is None else shlex.quote(
            base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        )
        result = self.machine.run(
            workspace_id,
            "python3 " + CONTROL + " " + args,
            timeout_seconds=36 if payload is not None else 10,
        )
        if result.get("exit_code") != 0:
            return {"ok": False, "error": "Sandbox browser control unavailable."}
        try:
            data = json.loads(result.get("stdout", "").strip())
            if not isinstance(data, dict):
                raise ValueError("Unexpected response")
            return data
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid browser control response."}

    def _view(self, workspace_id, token):
        # E2B requires an inbound traffic token that normal browser navigation
        # cannot attach. The authenticated Railway proxy injects it server-side.
        claims = self.machine.oauth.verify(workspace_id, "workspace")
        ticket = self.machine.oauth.sign(
            {"typ": "browser-view", "sandbox_id": claims["sandbox_id"]}, 1800
        )
        return self.machine.oauth.cfg.base_url.rstrip("/") + "/browser/" + ticket + "/#key=" + token

    def start(self, workspace_id: str, url: str = "https://example.com",
              viewport: str = "desktop") -> dict:
        validate_browser_input(url, "inspect", "", "")
        if viewport not in ("desktop", "mobile"):
            raise ValueError("Viewport must be desktop or mobile.")
        config = self._read_config(workspace_id)
        if config and config.get("token") and self._local(workspace_id).get("ready"):
            return {"ready": True, "reused": True, "viewport": config["viewport"],
                    "view_url": self._view(workspace_id, config["token"]),
                    "message": "Existing shared browser retained; use browser_stream_action to interact."}

        installed = self.machine.run(workspace_id, "python3 -c 'import playwright.sync_api'")
        if installed.get("exit_code") != 0:
            return {"ready": False, "error": "Playwright missing in E2B. Call browser_setup first."}

        # Upload gateway-reviewed server source into the sandbox.
        self.machine.write(workspace_id, SERVER, Path(__file__).with_name("stream_browser_server.py").read_text())
        self.machine.write(workspace_id, CONTROL, Path(__file__).with_name("stream_browser_control.py").read_text())
        config = {"token": secrets.token_urlsafe(36), "url": url, "viewport": viewport, "port": PORT}
        self.machine.write(workspace_id, CONFIG, json.dumps(config))
        self.machine.run(workspace_id, "chmod 600 " + CONFIG)
        launched = self.machine.start_process(
            workspace_id, "python3 " + SERVER + " --config " + CONFIG +
            " > " + SESSION_DIR + "/runtime.log 2>&1"
        )
        # Capture PID in the private file for owner-initiated teardown.
        if launched.get("pid") is not None:
            config["pid"] = int(launched["pid"])
            self.machine.write(workspace_id, CONFIG, json.dumps(config))
        # Startup can take a few seconds while Chromium launches.
        # Poll the same signed, loopback-only endpoint used for subsequent actions.
        state = {"ready": False}
        for _ in range(15):
            state = self._local(workspace_id)
            if state.get("ready") or state.get("failure"):
                break
            time.sleep(0.6)
        if not state.get("ready"):
            return {"ready": False, "error": "Browser process started but Chromium is not ready.",
                    "pid": launched.get("pid"), "failure": state.get("failure", "")}
        return {"ready": True, "reused": False, "viewport": viewport,
                "pid": launched.get("pid"),
                "view_url": self._view(workspace_id, config["token"]),
                "message": "Owner-only stream. Keep the access link private."}

    def status(self, workspace_id: str) -> dict:
        if not self._read_config(workspace_id):
            return {"ready": False, "error": "No browser stream started in this workspace."}
        return self._local(workspace_id)

    def action(self, workspace_id: str, action: str,
               selector: str = "", value: str = "",
               x: float | None = None, y: float | None = None,
               dy: float | None = None) -> dict:
        config = self._read_config(workspace_id)
        if not config:
            return {"ok": False, "error": "Start a browser stream first."}
        size = (390, 844) if config["viewport"] == "mobile" else (1440, 900)
        payload = {"action": action, "selector": selector, "value": value,
                   "x": x, "y": y, "dy": dy}
        validated = validate_action(payload, size)
        return self._local(workspace_id, validated)

    def stop(self, workspace_id: str) -> dict:
        config = self._read_config(workspace_id)
        if not config:
            return {"stopped": False, "error": "No browser stream exists."}
        pid = config.get("pid")
        if not isinstance(pid, int) or pid < 1:
            return {"stopped": False, "error": "Browser process PID unavailable."}
        stopped = self.machine.stop_process(workspace_id, pid)
        # Don't leave an active bearer token inside the sandbox after teardown.
        self.machine.run(workspace_id, "rm -f " + CONFIG, timeout_seconds=10)
        return {"stopped": bool(stopped.get("stopped")), "pid": pid}
