"""Desktop service lifecycle in an owner-authenticated E2B workspace."""
from __future__ import annotations

import json
import time
from pathlib import Path

from desktop_runtime import PORT

SOURCE = "/home/user/.devmcp-desktop/runtime.py"
STATE = "/home/user/.devmcp-desktop/ready.json"
LOG = "/home/user/.devmcp-desktop/desktop.log"


class DesktopManager:
    def __init__(self, machine):
        self.machine = machine

    def _status(self, workspace_id):
        script = (
            "import json,pathlib,socket;"
            "p=pathlib.Path(" + repr(STATE) + ");"
            "o={'ready':False};"
            "exec('if p.exists():\\n"
            " try:\\n  a=json.loads(p.read_text());"
            "\\n  with socket.create_connection((\\\"127.0.0.1\\\","
            + str(PORT) + "),.8): pass"
            "\\n  o=a\\n except Exception: pass');"
            "print(json.dumps(o))"
        )
        result = self.machine.run(
            workspace_id,
            "python3 -c " + __import__("shlex").quote(script),
            timeout_seconds=8,
        )
        try:
            return json.loads(result["stdout"].strip())
        except (KeyError, ValueError, TypeError):
            return {"ready": False}

    def setup(self, workspace_id):
        command = (
            "sudo -n apt-get update -qq && "
            "sudo -n apt-get install -y -qq xvfb fluxbox x11vnc novnc websockify xterm scrot >/tmp/devmcp-desktop-apt.log 2>&1 && "
            "command -v Xvfb && command -v x11vnc && command -v websockify"
        )
        result = self.machine.run(workspace_id, command, timeout_seconds=280)
        return {"installed": result.get("exit_code") == 0, **result}

    def _viewer(self, workspace_id):
        claims = self.machine.oauth.verify(workspace_id, "workspace")
        ticket = self.machine.oauth.sign({
            "typ": "desktop-view", "sandbox_id": claims["sandbox_id"]
        }, 1200)
        root = self.machine.oauth.cfg.base_url.rstrip("/")
        path = "desktop/" + ticket + "/websockify"
        return root + "/desktop/" + ticket + "/vnc_lite.html?autoconnect=1&path=" + path

    def start(self, workspace_id):
        status = self._status(workspace_id)
        if status.get("ready"):
            return {
                "ready": True, "reused": True,
                "viewer_url": self._viewer(workspace_id),
                "display": status.get("display", ":99"),
            }
        if self.machine.run(
            workspace_id, "command -v Xvfb && command -v x11vnc && command -v websockify",
            timeout_seconds=8,
        ).get("exit_code") != 0:
            return {"ready": False, "error": "Desktop packages unavailable. Run desktop_setup."}
        self.machine.run(workspace_id, "mkdir -p /home/user/.devmcp-desktop && chmod 700 /home/user/.devmcp-desktop")
        self.machine.write(
            workspace_id, SOURCE,
            Path(__file__).with_name("desktop_runtime.py").read_text(),
        )
        launched = self.machine.start_process(
            workspace_id,
            "python3 " + SOURCE + " > " + LOG + " 2>&1",
        )
        for _ in range(16):
            status = self._status(workspace_id)
            if status.get("ready"):
                return {
                    "ready": True, "reused": False,
                    "pid": status.get("pid"),
                    "display": status.get("display"),
                    "viewer_url": self._viewer(workspace_id),
                    "message": "Owner-only remote desktop; treat the viewing link as a password.",
                }
            time.sleep(0.5)
        return {
            "ready": False, "error": "Virtual desktop did not become ready",
            "start_pid": launched.get("pid"),
        }

    def status(self, workspace_id):
        return self._status(workspace_id)

    def stop(self, workspace_id):
        # Only terminate the supervisor process whose ID was recorded by
        # this desktop service. It terminates its own process group children.
        script = (
            "import json,os,pathlib,signal;"
            "p=pathlib.Path(" + repr(STATE) + ");"
            "data=json.loads(p.read_text());"
            "pid=int(data['pid']);"
            "assert pid>1;"
            "os.kill(pid,signal.SIGTERM);"
            "print('stopped')"
        )
        response = self.machine.run(
            workspace_id, "python3 -c " + __import__("shlex").quote(script),
            timeout_seconds=10,
        )
        return {"stopped": response.get("exit_code") == 0}
