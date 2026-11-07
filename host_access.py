"""Restricted Refit host SSH connector, independent of E2B workspace lifetimes.

Only the single configured Vultr VM is supported. No arbitrary remote
command, SSH port forwarding, password login or shell is exposed through MCP.
The client private key lives on an explicitly mounted Railway persistent volume.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HOST = "80.240.30.92"
USER = "root"
VALIDITY_SECONDS = 5 * 3600
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}\Z")
REVISION = re.compile(r"[a-f0-9]{40}\Z")
KEY_COMMENT = "refit-devmcp-gateway"
COMMAND_PATH = "/usr/local/sbin/refit-host-ssh-command"
MAX_OUTPUT = 6000


class HostAccessProblem(ValueError):
    pass


def key_fingerprint(blob: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def probe_host() -> tuple[str, str]:
    """Obtain the host key for comparison to a fingerprint supplied by the owner.

    Never trust a key merely because a remote server returned it.
    """
    sock = socket.create_connection((HOST, 22), timeout=8)
    transport = paramiko.Transport(sock)
    try:
        # Refuse RSA fallback: only the independently verified Ed25519 identity.
        transport.get_security_options().key_types = ("ssh-ed25519",)
        transport.start_client(timeout=10)
        key = transport.get_remote_server_key()
        if key.get_name() != "ssh-ed25519":
            raise HostAccessProblem("Expected an Ed25519 SSH host key.")
        blob = key.asbytes()
        return key.get_base64(), key_fingerprint(blob)
    finally:
        transport.close()


class HostAccess:
    """Owner-only host management through the already-authenticated MCP gateway."""

    def __init__(self, state_dir: str | None = None, *, require_volume: bool = True,
                 host_probe=None, ssh_factory=None, now=None):
        self.state_dir = Path(state_dir) if state_dir else None
        self.require_volume = require_volume
        self.host_probe = host_probe or probe_host
        self.ssh_factory = ssh_factory or paramiko.SSHClient
        self.now = now or time.time
        self.lock = threading.RLock()

    @classmethod
    def from_env(cls) -> "HostAccess":
        # No Railway variable is needed if the owner already mounted /data.
        # configured() still fails closed when /data is not a real volume.
        return cls(os.getenv("HOST_SSH_STATE_DIR", "").strip() or "/data/refit-host-ssh")

    def configured(self) -> bool:
        if self.state_dir is None or not self.state_dir.is_absolute():
            return False
        if self.require_volume:
            # The image includes /data as an ordinary directory. Without a
            # mounted volume keys would vanish on Railway redeployment.
            if not self.state_dir.is_relative_to(Path("/data")) or not os.path.ismount("/data"):
                return False
        return True

    def _directory(self) -> Path:
        if not self.configured():
            raise HostAccessProblem(
                "Host SSH is disabled. Attach a persistent Railway volume at /data "
                "and set HOST_SSH_STATE_DIR=/data/refit-host-ssh."
            )
        assert self.state_dir is not None
        root = self.state_dir
        if root.exists():
            if root.is_symlink() or not root.is_dir():
                raise HostAccessProblem("Refusing a symlink or non-directory host SSH state path.")
        else:
            root.mkdir(parents=True, mode=0o700, exist_ok=False)
        root.chmod(0o700)
        return root

    def _paths(self) -> tuple[Path, Path]:
        root = self._directory()
        return root / "identity_ed25519", root / "host-key.json"

    @staticmethod
    def _private(path: Path) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise HostAccessProblem("Missing or unsafe persistent SSH identity.")
        metadata = path.stat()
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise HostAccessProblem("SSH identity must have permissions 0600.")
        if metadata.st_size > 8000:
            raise HostAccessProblem("SSH identity exceeds the expected size.")
        return path.read_bytes()

    @staticmethod
    def _write_once(path: Path, content: bytes) -> None:
        # Exclusive creation never overwrites a different private key.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            raise

    def _load_record(self, path: Path) -> dict:
        if path.is_symlink() or not path.is_file():
            raise HostAccessProblem("SSH host is not initialized.")
        if path.stat().st_size > 4000 or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise HostAccessProblem("Unsafe SSH host-key record.")
        data = json.loads(path.read_text())
        if (data.get("host") != HOST or data.get("key_type") != "ssh-ed25519"
                or not isinstance(data.get("fingerprint"), str)
                or not isinstance(data.get("host_key"), str)
                or not isinstance(data.get("authorized_until"), int)):
            raise HostAccessProblem("Unrecognized SSH host-key record.")
        blob = base64.b64decode(data["host_key"], validate=True)
        if key_fingerprint(blob) != data["fingerprint"]:
            raise HostAccessProblem("SSH host-key fingerprint integrity check failed.")
        return data

    def _identity(self, key_path: Path) -> tuple[str, str]:
        private = serialization.load_ssh_private_key(self._private(key_path), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise HostAccessProblem("Unexpected private SSH key algorithm.")
        public = private.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        ).decode()
        key = paramiko.Ed25519Key.from_private_key_file(str(key_path))
        return public + " " + KEY_COMMENT, key_fingerprint(key.asbytes())

    def prepare(self, expected_fingerprint: str) -> dict:
        """Pin an independently checked server fingerprint, create a durable client key.

        The returned public key and paste command are safe to display. The
        private key never leaves persistent server-side storage.
        """
        if not isinstance(expected_fingerprint, str) or not FINGERPRINT.fullmatch(expected_fingerprint):
            raise HostAccessProblem("Provide the Ed25519 host SHA256 fingerprint from the Vultr console.")
        with self.lock:
            key_path, record_path = self._paths()
            key_b64, observed = self.host_probe()
            if observed != expected_fingerprint:
                raise HostAccessProblem("SSH host identity differs from the expected pinned fingerprint.")
            if key_path.exists() and not record_path.exists():
                raise HostAccessProblem("Incomplete persisted identity. Refusing to replace a private key.")
            if record_path.exists():
                record = self._load_record(record_path)
                if record["fingerprint"] != expected_fingerprint or record["host_key"] != key_b64:
                    raise HostAccessProblem("Host key changed. Manual review is required; refusing silent rotation.")
            else:
                private = Ed25519PrivateKey.generate()
                payload = private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.OpenSSH,
                    serialization.NoEncryption(),
                )
                self._write_once(key_path, payload)
                record = {"host": HOST, "key_type": "ssh-ed25519",
                          "host_key": key_b64, "fingerprint": expected_fingerprint,
                          "authorized_until": 0}
                self._write_once(record_path, (json.dumps(record) + "\n").encode())
            public, client_fp = self._identity(key_path)
            record["authorized_until"] = 0
            record_path.write_text(json.dumps(record) + "\n")
            record_path.chmod(0o600)
            # No local expiry is activated until the owner confirms the
            # server-side authorization by a successful authenticated status.
            return {
                "host": HOST, "host_fingerprint": expected_fingerprint,
                "client_public_key": public, "client_fingerprint": client_fp,
                "authorization_minutes": 300,
                "host_setup": "Run the root-only setup helper on Vultr with this public key: "
                              "cd /opt/refit-app && git pull --ff-only origin main && "
                              "bash deploy/setup-devmcp-host-access.sh '" + public + "'",
                "persisted": True, "authorized": False,
                "note": "Verify the server fingerprint independently. Never share SSH private keys."
            }

    def _connect(self, *, require_authorized: bool) -> tuple[object, dict]:
        key_path, record_path = self._paths()
        record = self._load_record(record_path)
        if require_authorized and self.now() >= record["authorized_until"]:
            raise HostAccessProblem("Five-hour SSH authorization expired. Reauthorize on Vultr.")
        key = paramiko.Ed25519Key.from_private_key_file(str(key_path))
        client = self.ssh_factory()
        host_key = paramiko.Ed25519Key(data=base64.b64decode(record["host_key"], validate=True))
        client.get_host_keys().add(HOST, "ssh-ed25519", host_key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(HOST, port=22, username=USER, pkey=key, timeout=9,
                           auth_timeout=10, banner_timeout=9, look_for_keys=False, allow_agent=False)
            return client, record
        except BaseException:
            client.close()
            raise

    def _execute(self, command: str, *, authorized: bool = True, timeout: int = 25) -> dict:
        client, record = self._connect(require_authorized=authorized)
        try:
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            # The remote forced-command script must decide what to execute.
            raw_out = stdout.read(MAX_OUTPUT + 1)
            raw_err = stderr.read(MAX_OUTPUT + 1)
            exit_code = stdout.channel.recv_exit_status()
            if len(raw_out) > MAX_OUTPUT or len(raw_err) > MAX_OUTPUT:
                raise HostAccessProblem("Host output exceeds the permitted boundary.")
            return {"exit_code": exit_code,
                    "stdout": raw_out.decode("utf-8", "replace"),
                    "stderr": raw_err.decode("utf-8", "replace"),
                    "host": HOST, "fingerprint": record["fingerprint"]}
        finally:
            client.close()

    def status(self) -> dict:
        """Check host state. Initial successful authentication begins local expiry."""
        with self.lock:
            key_path, record_path = self._paths()
            record = self._load_record(record_path)
            if record["authorized_until"] and self.now() >= record["authorized_until"]:
                raise HostAccessProblem("Five-hour authorization expired. Reauthorize via the Vultr console.")
            result = self._execute("status", authorized=False)
            if result["exit_code"] != 0:
                return {**result, "authorized": False}
            if not record["authorized_until"]:
                # Conservatively treat the current time as the start of the
                # user's server-side expiry clock. Server may expire sooner.
                record["authorized_until"] = int(self.now() + VALIDITY_SECONDS)
                record_path.write_text(json.dumps(record) + "\n")
                record_path.chmod(0o600)
            result["authorized"] = True
            result["local_authorized_until"] = datetime.fromtimestamp(
                record["authorized_until"], tz=timezone.utc
            ).isoformat()
            return result

    def deploy(self, revision: str) -> dict:
        """Deploy only a pinned 40-character Git SHA; no user-controlled shell."""
        if not isinstance(revision, str) or not REVISION.fullmatch(revision):
            raise HostAccessProblem("Deploy requires an exact lowercase 40-character Git SHA.")
        with self.lock:
            return self._execute("deploy " + revision, timeout=25 * 60)
