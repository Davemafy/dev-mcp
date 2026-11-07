"""Tests for owner-scoped full desktop and model-written adaptive browser plans."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop_runtime import PROCESSES, PORT, VNC_PORT
from desktop_manager import DesktopManager
from adaptive_browser import AdaptiveBrowser, validate_program


class FakeOAuth:
    cfg = SimpleNamespace(base_url="https://gateway.example")
    def verify(self, token, typ):
        assert token == "signed-workspace" and typ == "workspace"
        return {"sandbox_id": "sandbox-test-123"}
    def sign(self, data, seconds):
        assert data == {"typ": "desktop-view", "sandbox_id": "sandbox-test-123"}
        assert seconds == 1200
        return "private-desktop-ticket"


class FakeMachine:
    oauth = FakeOAuth()
    def __init__(self, ready=True):
        self.ready = ready
        self.commands = []
        self.files = {}
    def run(self, workspace_id, command, timeout_seconds=120):
        assert workspace_id == "signed-workspace"
        self.commands.append(command)
        if "command -v Xvfb" in command:
            return {"exit_code": 0, "stdout": "/usr/bin/Xvfb\n"}
        if "/ready.json" in command:
            return {"exit_code": 0, "stdout":
                    '{"ready":true,"display":":99","desktop_port":3488,"pid":1212}'
                    if self.ready else '{"ready":false}'}
        return {"exit_code": 0, "stdout": ""}
    def write(self, workspace_id, path, content):
        assert workspace_id == "signed-workspace"
        self.files[path] = content
    def start_process(self, workspace_id, command):
        assert workspace_id == "signed-workspace"
        self.commands.append(command)
        self.ready = True
        return {"started": True, "pid": 1212}


def test_runtime_is_real_vnc_desktop_but_processes_remain_private():
    assert PORT == 3488 and VNC_PORT == 5901
    assert PROCESSES["display"][:2] == ["Xvfb", ":99"]
    assert "-localhost" in PROCESSES["vnc"]
    assert "-forever" in PROCESSES["vnc"]
    assert "-nopw" in PROCESSES["vnc"]  # Gateway bearer ticket is the auth boundary.
    assert "--web" in PROCESSES["websocket"]
    assert "0.0.0.0:3488" in PROCESSES["websocket"]
    assert PROCESSES["websocket"][-1] == "127.0.0.1:5901"


def test_desktop_reuse_and_private_view_ticket():
    manager = DesktopManager(FakeMachine(ready=True))
    result = manager.start("signed-workspace")
    assert result["ready"] and result["reused"]
    assert "https://gateway.example/desktop/private-desktop-ticket/vnc_lite.html" in result["viewer_url"]
    assert "path=desktop/private-desktop-ticket/websockify" in result["viewer_url"]
    assert "password=" not in result["viewer_url"]


def test_desktop_setup_and_new_start_are_scoped_to_signed_workspace():
    machine = FakeMachine(ready=False)
    manager = DesktopManager(machine)
    setup = manager.setup("signed-workspace")
    assert setup["installed"]
    result = manager.start("signed-workspace")
    assert result["ready"] and not result["reused"]
    assert result["pid"] == 1212
    assert "/home/user/.devmcp-desktop/runtime.py" in machine.files
    assert any("python3 /home/user/.devmcp-desktop/runtime.py" in cmd for cmd in machine.commands)
    assert any("scrot" in cmd for cmd in machine.commands)


def test_model_written_program_failure_returns_live_evidence_for_repair():
    class Browser:
        def __init__(self):
            self.actions = []
        def status(self, workspace_id):
            return {"ready": True, "width": 1440, "height": 900}
        def action(self, workspace_id, action, **kwargs):
            self.actions.append((action, kwargs))
            if action == "inspect_dom":
                return {"ok": True, "elements": [
                    {"tag": "button", "name": "Continue", "testId": "go-2026"},
                ], "url": "http://127.0.0.1:3000", "frame_version": 3}
            if kwargs.get("selector") == "#old-selector":
                return {"ok": False, "error": "TimeoutError"}
            return {"ok": True, "url": "http://127.0.0.1:3000", "frame_version": 4}

    browser = Browser()
    runner = AdaptiveBrowser(browser)
    first = runner.execute("signed", [
        {"action": "click", "selector": "#old-selector"},
    ])
    assert first["ok"] is False and first["failed_at"] == 1
    assert first["observation"]["elements"][0]["testId"] == "go-2026"
    fixed = runner.execute("signed", [
        {"action": "click", "selector": "[data-testid=go-2026]"},
        {"action": "assert_text", "selector": "body", "value": "Done"},
    ])
    assert fixed["ok"] and fixed["verified"]
    assert len(fixed["trace"]) == 2


def test_model_program_has_bounded_capabilities():
    assert validate_program([{"action": "inspect_dom"}], (1440, 900)) == [{"action": "inspect_dom"}]
    with pytest.raises(ValueError):
        validate_program([{"action": "evaluate", "script": "fetch('/secret')"}], (1440, 900))
    with pytest.raises(ValueError):
        validate_program([{"action": "click", "selector": "button"}] * 13, (1440, 900))
    with pytest.raises(ValueError):
        validate_program([{"action": "click_at", "x": 9000, "y": 1}], (1440, 900))


def test_headful_streaming_browser_shares_x11_desktop():
    server = Path("stream_browser_server.py").read_text()
    manager = Path("stream_browser.py").read_text()
    assert 'os.getenv("DEVMCP_HEADFUL") == "1"' in server
    assert '["scrot", "-q", "50", str(CAPTURE)]' in server
    assert "DEVMCP_HEADFUL=1" in manager
    assert 'action == "inspect_dom"' in server
    assert "wait_for" in server


def test_gateway_declares_all_desktop_and_adaptive_tools():
    source = Path("main.py").read_text()
    for tool in ("desktop_start", "desktop_stop", "desktop_status", "desktop_setup",
                 "desktop_browser_open", "browser_observe", "browser_adaptive_run"):
        assert "def " + tool + "(" in source
    assert "WebSocketRoute('/desktop/{ticket}/websockify'" in source
