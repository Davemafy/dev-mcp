"""Restricted Refit SSH fallback using an owner-authenticated E2B workspace.

Railway's gateway can be unable to reach TCP/22 even when its E2B sandbox can.
This fallback uses a short-lived SSH identity generated ONLY inside that
disposable workspace. It never exports the private key to Railway or ChatGPT.

The Refit server must install this *public* key using its existing restricted,
five-hour forced-command setup. No general-purpose host command is exposed.
"""
from __future__ import annotations

import json
import re
import shlex

from host_access import HOST, FINGERPRINT, REVISION, HostAccessProblem

_DIR = "/home/user/.devmcp-refit-ssh"
_IDENTITY = _DIR + "/identity_ed25519"
_KNOWN = _DIR + "/known_hosts"

_PREPARE = r"""
import base64, hashlib, json, os, pathlib, stat, subprocess, sys

host, expected = sys.argv[1:]
result = subprocess.run(
    ["ssh-keyscan", "-T", "8", "-t", "ed25519", host],
    text=True, capture_output=True, timeout=13, check=False)
keys = set()
for line in result.stdout.splitlines():
    fields = line.split()
    if len(fields) == 3 and fields[0] == host and fields[1] == "ssh-ed25519":
        keys.add(fields[2])
if len(keys) != 1:
    raise RuntimeError("No unique Ed25519 SSH host key; check workspace connectivity")
raw_key = next(iter(keys))
blob = base64.b64decode(raw_key, validate=True)
observed = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
if observed != expected:
    raise RuntimeError("SSH host fingerprint differs from owner-verified Vultr fingerprint")

directory = pathlib.Path("/home/user/.devmcp-refit-ssh")
if directory.is_symlink():
    raise RuntimeError("Refusing symlinked SSH directory")
directory.mkdir(mode=0o700, exist_ok=True)
if not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
    raise RuntimeError("SSH directory must be private (0700)")
known = directory / "known_hosts"
line = host + " ssh-ed25519 " + raw_key + "\n"
if known.exists() or known.is_symlink():
    if known.is_symlink() or not known.is_file() or known.read_text() != line:
        raise RuntimeError("Pinned SSH host key changed; refusing rotation")
    if stat.S_IMODE(known.stat().st_mode) != 0o600:
        raise RuntimeError("SSH known_hosts must be private (0600)")
else:
    fd = os.open(known, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())

identity = directory / "identity_ed25519"
public = directory / "identity_ed25519.pub"
if identity.exists() or identity.is_symlink() or public.exists() or public.is_symlink():
    if (identity.is_symlink() or public.is_symlink() or not identity.is_file()
            or not public.is_file() or stat.S_IMODE(identity.stat().st_mode) != 0o600):
        raise RuntimeError("Existing SSH identity is incomplete or unsafe")
else:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
         "refit-devmcp-gateway", "-f", str(identity)],
        capture_output=True, text=True, check=True, timeout=15)
pub = public.read_text().strip()
parts = pub.split()
if len(parts) != 3 or parts[0] != "ssh-ed25519" or parts[2] != "refit-devmcp-gateway":
    raise RuntimeError("Unexpected SSH client public key")
print(json.dumps({"client_public_key": pub, "host_fingerprint": observed}))
"""

def prepare(machine, workspace_id: str, expected_fingerprint: str) -> dict:
    """Pin the owner's verified host fingerprint and generate a disposable client key."""
    if not isinstance(expected_fingerprint, str) or not FINGERPRINT.fullmatch(expected_fingerprint):
        raise HostAccessProblem("Provide the SHA256 Ed25519 host fingerprint from the Vultr console.")
    command = ("python3 -c " + shlex.quote(_PREPARE) + " "
               + shlex.quote(HOST) + " " + shlex.quote(expected_fingerprint))
    result = machine.run(workspace_id, command, timeout_seconds=40)
    if result["exit_code"] != 0:
        raise HostAccessProblem("Workspace SSH preparation failed: "
                                + result.get("stderr", "")[-1000:])
    try:
        record = json.loads(result["stdout"])
        public = record["client_public_key"]
        if (not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]{60,120} refit-devmcp-gateway",
                             public) or record["host_fingerprint"] != expected_fingerprint):
            raise ValueError("Unexpected workspace key response")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HostAccessProblem("Invalid workspace SSH key response") from exc
    return {
        "host": HOST,
        "host_fingerprint": expected_fingerprint,
        "client_public_key": public,
        "authorized": False,
        "persistent": False,
        "workspace_lifetime": "Key is lost when the E2B workspace expires or closes.",
        "authorization_minutes": 300,
        "host_setup": (
            "On the Vultr console, run: "
            "cd /opt/refit-app && "
            "bash deploy/setup-devmcp-host-access.sh " + shlex.quote(public)
        ),
        "note": "No private key leaves E2B. Existing server forced-command restrictions apply."
    }


def status(machine, workspace_id: str) -> dict:
    """Authenticate with the pinned host through the E2B network path."""
    # No caller-controlled command, hostname, username, SSH options, or file path.
    command = " ".join(shlex.quote(arg) for arg in (
        "ssh", "-F", "/dev/null", "-i", _IDENTITY,
        "-o", "UserKnownHostsFile=" + _KNOWN,
        "-o", "StrictHostKeyChecking=yes",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "ConnectTimeout=9",
        "-o", "ClearAllForwardings=yes",
        "root@" + HOST, "status"))
    result = machine.run(workspace_id, command, timeout_seconds=30)
    return {**result, "host": HOST, "authorized": result["exit_code"] == 0}


def deploy(machine, workspace_id: str, revision: str) -> dict:
    """Deploy only a full pinned Refit Git SHA via the server's forced command."""
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise HostAccessProblem("Deploy requires an exact lowercase 40-character Git SHA.")
    command = " ".join(shlex.quote(arg) for arg in (
        "ssh", "-F", "/dev/null", "-i", _IDENTITY,
        "-o", "UserKnownHostsFile=" + _KNOWN,
        "-o", "StrictHostKeyChecking=yes",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "ConnectTimeout=9",
        "-o", "ClearAllForwardings=yes",
        "root@" + HOST, "deploy " + revision))
    # The generic workspace command runner has a bounded execution timeout.
    # A deploy that exceeds this returns a timeout, not a claimed success.
    result = machine.run(workspace_id, command, timeout_seconds=300)
    return {**result, "host": HOST}
