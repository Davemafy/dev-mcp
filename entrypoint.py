"""Prepare a Railway-mounted volume, then irrevocably drop root privileges.

Railway mounts volumes as root. We use root only for one initial ownership
change, then run OAuth, FastMCP and SSH client operations as UID 10001.
"""
from __future__ import annotations

import os
import sys

APP_UID = 10001


def start() -> None:
    mounted = os.path.ismount("/data")
    if os.geteuid() == 0:
        if mounted:
            os.chown("/data", APP_UID, APP_UID)
            os.chmod("/data", 0o700)
        os.setgroups([])
        os.setgid(APP_UID)
        os.setuid(APP_UID)
    elif mounted and not os.access("/data", os.W_OK | os.X_OK):
        raise RuntimeError("Railway volume at /data is not writable by the application UID.")
    if os.geteuid() != APP_UID:
        raise RuntimeError("Refusing to run the gateway with root privileges.")
    os.execv(sys.executable, [sys.executable, "/app/main.py"])


if __name__ == "__main__":
    start()
