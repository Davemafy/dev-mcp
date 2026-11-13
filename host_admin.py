"""Owner-authorized SSH access from the isolated worker to its Vultr host.

The private SSH key is generated on the VM by an owner-run bootstrap script
and mounted read-only *outside* /workspaces. Development jobs run as UID 10001
and cannot read it. The gateway requires owner OAuth and a long VM worker token.
SSH pins the host's own Ed25519 identity; no TOFU, agent forwarding or password.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import tempfile

STATE = Path("/etc/devmcp/host-admin")
HOST = "host.docker.internal"
USER = "devmcp-admin"
MAX_OUTPUT = 30000


def _ready():
    key = STATE / "identity_ed25519"
    known = STATE / "known_hosts"
    if (STATE.is_symlink() or not STATE.is_dir()
            or any(p.is_symlink() or not p.is_file() for p in (key, known))):
        return False
    return (stat.S_IMODE(key.stat().st_mode) == 0o600
            and key.stat().st_uid == 0
            and known.stat().st_size < 4096
            and key.stat().st_size < 4096
            and known.read_text().startswith(HOST + " ssh-ed25519 "))


def admin_status():
    configured = _ready()
    if not configured:
        return {"configured": False, "connected": False,
                "message": "Run deploy/setup-vm-host-admin.sh on Vultr, then recreate the DevMCP worker."}
    result = run_host_command("id -u; nproc; awk '/MemTotal/ {print $2}' /proc/meminfo", 20)
    return {"configured": True, "connected": result["exit_code"] == 0,
            "host": HOST, "exit_code": result["exit_code"],
            "stdout": result["stdout"], "stderr": result["stderr"]}


def run_host_command(command, timeout=90):
    if not _ready():
        raise ValueError("Owner-authorized host SSH is not configured.")
    if not isinstance(command, str) or not 1 <= len(command) <= 10000 or "\x00" in command:
        raise ValueError("Invalid host command")
    timeout = max(1, min(int(timeout), 600))
    # The shell only interprets the quoted command on the SSH destination.
    args = [
        "ssh", "-F", "/dev/null", "-T",
        "-i", str(STATE / "identity_ed25519"),
        "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=" + str(STATE / "known_hosts"),
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=9",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        USER + "@" + HOST,
        "sudo", "-n", "--", "/bin/bash", "-lc", shlex.quote(command),
    ]
    # Audit a digest, not the command: secrets may appear in user input.
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
    print("host-admin action", digest, "requested", flush=True)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, close_fds=True,
            start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=10)
            rc = 124
        stdout.seek(0)
        stderr.seek(0)
        out = stdout.read(MAX_OUTPUT + 1)
        err = stderr.read(MAX_OUTPUT + 1)
    print("host-admin result", digest, "exit", rc, flush=True)
    return {
        "exit_code": rc,
        "stdout": out[:MAX_OUTPUT].decode("utf-8", "replace"),
        "stderr": err[:MAX_OUTPUT].decode("utf-8", "replace"),
        "stdout_truncated": len(out) > MAX_OUTPUT,
        "stderr_truncated": len(err) > MAX_OUTPUT,
        "host": HOST,
        "command_sha256_prefix": digest,
    }
