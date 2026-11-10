"""Dedicated Vultr execution worker; outbound HTTPS only, no AI model.

Run inside the restricted devmcp-vm-worker Docker container, never directly
on the Refit host. Development commands run as UID 10001 with a sanitized
environment; the worker's Railway token remains in the control process.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path("/workspaces")
IDENT = re.compile(r"^[a-f0-9]{32}$")
UID = 10001
GID = 10001
RUNNING = {}
SAFE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def _child_identity():
    """Avoid Python preexec_fn, which is unsafe with heartbeat threads."""
    if os.geteuid() == 0:
        return {"user": UID, "group": GID, "extra_groups": []}
    if os.geteuid() == UID and os.getegid() == GID:
        return {}
    raise RuntimeError("Worker must have permission to run commands as the isolated user")


def _root(identifier):
    if not isinstance(identifier, str) or not IDENT.fullmatch(identifier):
        raise ValueError("Invalid workspace ID")
    root = ROOT / identifier
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unknown workspace ID")
    if not root.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Workspace escapes workspaces directory")
    return root


def _file(root, value):
    if not isinstance(value, str) or not value or len(value) > 1000 or "\x00" in value:
        raise ValueError("Invalid file path")
    if value == "/home/user":
        value = "."
    elif value.startswith("/home/user/"):
        value = value[len("/home/user/"):]
    elif value.startswith("/"):
        raise ValueError("Absolute file path denied")
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("File path escapes workspace")
    return path


def _command(root, command):
    if not isinstance(command, str) or not 1 <= len(command) <= 10000:
        raise ValueError("Invalid command")
    env = {**SAFE_ENV, "HOME": str(root)}
    return ["/bin/sh", "-lc", command], env


def run_command(root, command, timeout=90):
    args, env = _command(root, command)
    timeout = max(1, min(int(timeout), 180))
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(
            args, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, close_fds=True,
            start_new_session=True, **_child_identity(),
        )
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=10)
            return {"exit_code": 124, "stdout": "", "stderr": "Command timed out"}
        stdout.seek(0)
        stderr.seek(0)
        out = stdout.read(30001)
        err = stderr.read(30001)
        return {
            "exit_code": rc,
            "stdout": out[:30000].decode("utf-8", "replace"),
            "stderr": err[:30000].decode("utf-8", "replace"),
            "stdout_truncated": len(out) > 30000,
            "stderr_truncated": len(err) > 30000,
        }


def execute(action, args):
    if not isinstance(args, dict):
        raise ValueError("Invalid worker arguments")
    if action == "create":
        wid = secrets.token_hex(16)
        folder = ROOT / wid
        folder.mkdir(mode=0o700)
        os.chown(folder, UID, UID)
        return {"worker_id": wid}
    if action == "recover":
        return {"workspaces": sorted(
            p.name for p in ROOT.iterdir() if
            p.is_dir() and not p.is_symlink() and IDENT.fullmatch(p.name)
        )[:100]}
    wid = args.get("worker_id")
    root = _root(wid)
    if action == "status":
        return {"ready": True, "home": "/home/user", "persistent": True}
    if action == "close":
        for (workspace_id, pid), proc in list(RUNNING.items()):
            if workspace_id == wid:
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                RUNNING.pop((workspace_id, pid), None)
        shutil.rmtree(root)
        return {"closed": True}
    if action in ("read", "write", "list"):
        path = _file(root, args.get("path", "."))
        if action == "read":
            if path.stat().st_size > 100000:
                raise ValueError("File exceeds 100 KB read limit")
            raw = path.read_bytes()
            return {"path": str(path.relative_to(root)),
                    "content": raw[:30000].decode("utf-8", "replace"),
                    "truncated": len(raw) > 30000}
        if action == "write":
            data = args.get("content")
            if not isinstance(data, str) or len(data.encode()) > 500000:
                raise ValueError("File content exceeds 500 KB")
            # Give the unprivileged command user access to new nested dirs.
            missing = []
            parent = path.parent
            while parent != root and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir(mode=0o700)
                os.chown(directory, UID, UID)
            path.write_text(data, encoding="utf-8")
            os.chown(path, UID, UID)
            return {"path": str(path.relative_to(root)), "bytes": len(data.encode())}
        if not path.is_dir():
            raise ValueError("Not a directory")
        return {"files": [{
            "name": p.name, "type": "directory" if p.is_dir() else "file",
            "size": p.stat().st_size
        } for p in list(path.iterdir())[:100]]}
    if action == "run":
        return run_command(root, args.get("command"), args.get("timeout_seconds", 90))
    if action == "start":
        if sum(proc.poll() is None for (w, _), proc in RUNNING.items() if w == wid) >= 4:
            raise ValueError("Too many background processes")
        cmd, env = _command(root, args.get("command"))
        proc = subprocess.Popen(
            cmd, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True, **_child_identity(),
        )
        RUNNING[(wid, proc.pid)] = proc
        return {"pid": proc.pid, "started": True}
    if action == "processes":
        return {"processes": [{"pid": pid, "running": proc.poll() is None}
            for (w, pid), proc in list(RUNNING.items()) if w == wid]}
    if action == "stop":
        pid = int(args.get("pid", 0))
        proc = RUNNING.pop((wid, pid), None)
        if proc is None:
            raise ValueError("Unknown process")
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        return {"stopped": True, "pid": pid}
    raise ValueError("Unsupported action")


def _post(base, token, endpoint, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    req = Request(
        base + "/internal/vm/" + endpoint,
        data=raw, method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urlopen(req, timeout=25) as res:
        response = res.read(128_000)
        return json.loads(response)


def _heartbeat_loop(base, token):
    # Heartbeats must continue during long-running development commands.
    while True:
        try:
            _post(base, token, "heartbeat", {})
        except Exception:
            pass
        time.sleep(5)


def main():
    base = os.getenv("VM_GATEWAY_URL", "").rstrip("/")
    token = os.getenv("VM_WORKER_TOKEN", "")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.username or parsed.password:
        raise SystemExit("VM_GATEWAY_URL must be a fixed HTTPS origin")
    if len(token) < 32:
        raise SystemExit("VM_WORKER_TOKEN must be at least 32 characters")
    if not ROOT.is_dir() or not os.access(ROOT, os.W_OK):
        raise SystemExit("Worker workspace volume is missing or not writable")
    print("DevMCP Vultr worker started; outbound HTTPS only", flush=True)
    threading.Thread(target=_heartbeat_loop, args=(base, token),
                     daemon=True, name="vm-heartbeat").start()
    retry = 1
    while True:
        try:
            payload = _post(base, token, "claim", {})
            task = payload.get("task")
            if not task:
                retry = 1
                time.sleep(0.7)
                continue
            try:
                result = execute(task["action"], task["args"])
            except Exception as exc:
                result = {"error": type(exc).__name__, "message": str(exc)[:300]}
            # Report the SAME receipt until acknowledged; do not re-execute.
            while True:
                try:
                    receipt = _post(base, token, "report", {"id": task["id"], "result": result})
                    if receipt.get("accepted") is True or receipt.get("accepted") is False:
                        break
                except Exception:
                    time.sleep(2)
            retry = 1
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("VM worker transport issue:", type(exc).__name__, flush=True)
            time.sleep(min(retry, 20))
            retry = min(retry * 2, 20)


if __name__ == "__main__":
    main()
