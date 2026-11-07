"""Authenticated cloud stream authorization, persistent session and agent control."""
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from stream_browser_server import BrowserState, build_handler, validate_action, safe_url, HTML, JS
from stream_browser import BrowserStream


def fetch(base, path, method="GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(base + path, data=data, method=method,
                      headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(request, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read(20000)
    except HTTPError as error:
        return error.code, dict(error.headers), error.read(20000)


def test_scope_and_origin_protection():
    state = BrowserState("top-secret-" + "A" * 40, "https://example.com", "mobile")
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(state))
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = "http://127.0.0.1:" + str(server.server_port)
    try:
        assert fetch(base, "/state")[0] == 401
        assert fetch(base, "/stream")[0] == 401
        assert fetch(base, "/snapshot")[0] == 401
        assert fetch(base, "/")[0] == 200
        assert fetch(base, "/session", "POST", {"key": state.token})[0] == 403
        origin = {"Origin": "https://127.0.0.1:" + str(server.server_port)}
        assert fetch(base, "/session", "POST", {"key": "wrong"}, origin)[0] == 401
        code, headers, _ = fetch(base, "/session", "POST", {"key": state.token}, origin)
        assert code == 200
        assert "HttpOnly" in headers["Set-Cookie"]
        assert "SameSite=Strict" in headers["Set-Cookie"]
        assert "Secure" in headers["Set-Cookie"]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        assert state.token not in cookie
        authenticated = {"Cookie": cookie}
        assert fetch(base, "/state", headers=authenticated)[0] == 503
        assert fetch(base, "/control", "POST", {"action": "reload"}, authenticated)[0] == 403
        bearer = {"Authorization": "Bearer " + state.token}
        def process_one():
            item = state.instructions.get(timeout=4)
            action, result, done = item
            result.update({"ok": True, "action": action["action"]})
            done.set()
        task = threading.Thread(target=process_one, daemon=True)
        task.start()
        result = fetch(base, "/control", "POST", {"action": "reload"}, bearer)
        assert result[0] == 200
        assert json.loads(result[2])["ok"]
        task.join(timeout=3)
        state.frame = b"\xff\xd8imagefake"
        assert fetch(base, "/snapshot", headers=authenticated)[2] == b"\xff\xd8imagefake"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_validation_blocks_unsafe_navigation_and_coordinates():
    with pytest.raises(ValueError):
        safe_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        safe_url("http://u:p@example.com")
    with pytest.raises(ValueError):
        validate_action({"action": "click_at", "x": 391, "y": 10}, (390, 844))
    with pytest.raises(ValueError):
        validate_action({"action": "scroll", "dy": 9000}, (390, 844))
    with pytest.raises(ValueError):
        validate_action({"action": "javascript", "value": "alert(1)"}, (390, 844))
    assert validate_action({"action": "keypress", "value": "Enter"}, (390, 844))["value"] == "Enter"
    assert validate_action({"action": "click_at", "x": 200, "y": 300}, (390, 844))["x"] == 200


def test_html_has_no_untrusted_external_scripts_or_unsanitized_html():
    assert '<script src="app.js" defer>' in HTML
    assert "<iframe" not in HTML
    assert "eval(" not in JS
    assert "location.hash" in JS
    assert "history.replaceState" in JS
    assert "fetch('control'" in JS


def test_stream_manager_uses_same_workspace_and_bearer_restricted_local_control():
    class MockMachine:
        def __init__(self):
            self.data = {}
            class O:
                def __init__(self):
                    self.cfg = type("Cfg", (), {"base_url": "https://gateway.example"})()
                def verify(self, handle, kind):
                    assert handle == "signed" and kind == "workspace"
                    return {"sandbox_id": "sandbox-1234"}
                def sign(self, values, lifetime):
                    assert values["typ"] == "browser-view" and lifetime == 1800
                    return "signed-viewer"
            self.oauth = O()
            self.commands = []
            self.started = False

        def read(self, workspace_id, path):
            assert workspace_id == "signed"
            if path not in self.data:
                raise RuntimeError("not found")
            return {"content": self.data[path]}

        def write(self, workspace_id, path, content):
            assert workspace_id == "signed"
            self.data[path] = content
            return {"written": True}

        def run(self, workspace_id, command, timeout_seconds=120):
            assert workspace_id == "signed"
            self.commands.append(command)
            if "import playwright" in command:
                return {"exit_code": 0}
            if "/control.py" in command and "--status" in command:
                return {"exit_code": 0, "stdout": json.dumps({
                    "ready": self.started, "url": "https://example.com",
                    "title": "Example", "width": 1440, "height": 900
                })}
            if "/control.py" in command:
                return {"exit_code": 0, "stdout": json.dumps({"ok": True, "action": "click"})}
            return {"exit_code": 0, "stdout": ""}

        def start_process(self, workspace_id, command):
            assert workspace_id == "signed"
            self.started = True
            return {"started": True, "pid": 224}

        def preview_url(self, workspace_id, port):
            assert workspace_id == "signed"
            assert port == 3487
            return {"url": "https://demo.e2b.app", "port": port}

        def stop_process(self, workspace_id, pid):
            assert workspace_id == "signed"
            assert pid == 224
            self.started = False
            return {"stopped": True}

    machine = MockMachine()
    manager = BrowserStream(machine)
    result = manager.start("signed", "https://example.com")
    assert result["ready"] and not result["reused"]
    assert result["view_url"].startswith("https://gateway.example/browser/signed-viewer/#key=")
    assert "token" in machine.data["/home/user/.devmcp-stream/config.json"]
    assert machine.data["/home/user/.devmcp-stream/server.py"].startswith('"""Single-owner')
    assert manager.status("signed")["ready"]
    assert manager.action("signed", "click", selector="button")["ok"]
    assert manager.start("signed", "https://example.com")["reused"]
    assert manager.stop("signed")["stopped"]
