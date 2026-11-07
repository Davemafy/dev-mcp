"""Isolated X11 desktop with noVNC, supervised inside a signed E2B workspace.

The gateway handles authentication. x11vnc is loopback-only and websockify's
E2B preview port is traffic-token protected. Never deploy this on Railway host.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

DISPLAY = ":99"
PORT = 3488
VNC_PORT = 5901
HOME = Path("/home/user/.devmcp-desktop")
PROCESSES = {
    "display": ["Xvfb", DISPLAY, "-screen", "0", "1440x900x24", "-nolisten", "tcp", "-noreset"],
    "window_manager": ["fluxbox", "-display", DISPLAY],
    "vnc": ["x11vnc", "-display", DISPLAY, "-localhost", "-rfbport", str(VNC_PORT),
            "-forever", "-shared", "-nopw", "-noxdamage", "-quiet"],
    "websocket": ["websockify", "--web", "/usr/share/novnc",
                  "0.0.0.0:" + str(PORT), "127.0.0.1:" + str(VNC_PORT)],
}


def tcp_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.75):
            return True
    except (OSError, TimeoutError):
        return False


def serve() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = dict(os.environ, DISPLAY=DISPLAY, HOME="/home/user")
    handles: dict[str, subprocess.Popen] = {}

    try:
        for name, command in PROCESSES.items():
            log = open(HOME / (name + ".log"), "ab", buffering=0)
            handles[name] = subprocess.Popen(
                command, env=env, stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            if name == "display":
                time.sleep(1.2)
            elif name == "vnc":
                for _ in range(30):
                    if tcp_ready(VNC_PORT) or handles[name].poll() is not None:
                        break
                    time.sleep(0.3)
            elif name == "websocket":
                for _ in range(30):
                    if tcp_ready(PORT) or handles[name].poll() is not None:
                        break
                    time.sleep(0.3)
            if handles[name].poll() is not None:
                raise RuntimeError(name + " exited unexpectedly")

        if not tcp_ready(VNC_PORT) or not tcp_ready(PORT):
            raise RuntimeError("VNC or WebSocket bridge is not listening")

        (HOME / "ready.json").write_text(json.dumps({
            "ready": True, "display": DISPLAY, "desktop_port": PORT,
            "vnc_port": VNC_PORT, "pid": os.getpid(),
        }))
        print("DESKTOP_READY", flush=True)

        while True:
            for name, process in handles.items():
                if process.poll() is not None:
                    raise RuntimeError(name + " exited")
            time.sleep(1)

    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as error:
        print("DESKTOP_FAILURE " + type(error).__name__, flush=True)
        raise
    finally:
        try:
            (HOME / "ready.json").unlink(missing_ok=True)
        except OSError:
            pass
        for process in reversed(list(handles.values())):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass


if __name__ == "__main__":
    serve()
