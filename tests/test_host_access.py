"""Pure offline tests of persistent, owner-authenticated Refit SSH access.

Never open a real socket or execute a command on the production Vultr host.
"""
from __future__ import annotations
import base64
from io import BytesIO
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import paramiko
import pytest

from host_access import HostAccess, HostAccessProblem, HOST, key_fingerprint


class Output(BytesIO):
    def __init__(self, payload: bytes, code: int = 0):
        super().__init__(payload)
        self.channel = self
        self.code = code

    def recv_exit_status(self):
        return self.code


class FakeSSH:
    commands = []
    connections = []
    connect_error = None

    def __init__(self):
        self.keys = paramiko.HostKeys()
        self.closed = False

    def get_host_keys(self):
        return self.keys

    def set_missing_host_key_policy(self, policy):
        assert isinstance(policy, paramiko.RejectPolicy)

    def connect(self, hostname, **kwargs):
        if self.connect_error:
            raise self.connect_error
        assert hostname == HOST and kwargs["port"] == 22
        assert kwargs["username"] == "root"
        assert isinstance(kwargs["pkey"], paramiko.Ed25519Key)
        assert kwargs["look_for_keys"] is False and kwargs["allow_agent"] is False
        assert self.keys.lookup(HOST)["ssh-ed25519"] is not None
        self.connections.append(kwargs)

    def exec_command(self, command, timeout):
        assert command == "status" or command.startswith("deploy ")
        self.commands.append(command)
        return None, Output(b"Repository: 0123456789abcdef\nService: active\nPublic HTTP: 200\n"), Output(b"")

    def close(self):
        self.closed = True


@pytest.fixture
def server_key():
    key = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode().split()[1]
    blob = base64.b64decode(key, validate=True)
    return key, key_fingerprint(blob)


@pytest.fixture
def access(tmp_path, server_key):
    FakeSSH.commands = []
    FakeSSH.connections = []
    FakeSSH.connect_error = None
    clock = {"now": 10000}
    key, fingerprint = server_key
    manager = HostAccess(str(tmp_path / "host"), require_volume=False,
                         host_probe=lambda: (key, fingerprint),
                         ssh_factory=FakeSSH, now=lambda: clock["now"])
    return manager, clock, fingerprint


def test_prepare_creates_a_persistent_key_and_never_returns_private_material(access):
    m, _, fingerprint = access
    with pytest.raises(HostAccessProblem, match="fingerprint"):
        m.prepare("not-a-fingerprint")
    with pytest.raises(HostAccessProblem, match="differs"):
        m.prepare("SHA256:" + "A" * 43)
    result = m.prepare(fingerprint)
    assert result["persisted"] and not result["authorized"]
    assert result["host"] == HOST
    assert result["host_fingerprint"] == fingerprint
    assert result["client_public_key"].startswith("ssh-ed25519 ")
    assert "deploy/setup-devmcp-host-access.sh" in result["host_setup"]
    assert "BEGIN OPENSSH PRIVATE KEY" not in json.dumps(result)
    private = m.state_dir / "identity_ed25519"
    record = m.state_dir / "host-key.json"
    assert private.exists() and record.exists()
    assert private.stat().st_mode & 0o777 == 0o600
    assert record.stat().st_mode & 0o777 == 0o600
    assert m.state_dir.stat().st_mode & 0o777 == 0o700
    assert private.read_bytes().startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
    prior_key = private.read_bytes()
    renewed = m.prepare(fingerprint)
    assert renewed["client_public_key"] == result["client_public_key"]
    assert private.read_bytes() == prior_key


def test_status_deploy_and_expiry_are_not_arbitrary_shells(access):
    m, clock, fingerprint = access
    m.prepare(fingerprint)
    status = m.status()
    assert status["authorized"] and status["exit_code"] == 0
    assert status["stdout"].startswith("Repository: ")
    assert FakeSSH.commands == ["status"]
    before = m.state_dir / "host-key.json"
    assert json.loads(before.read_text())["authorized_until"] == 10000 + 18000
    sha = "a" * 40
    deployed = m.deploy(sha)
    assert deployed["exit_code"] == 0
    assert FakeSSH.commands == ["status", "deploy " + sha]
    for invalid in ("main", "a" * 39, "a" * 41, "x" * 40, "a" * 40 + " ; whoami"):
        with pytest.raises(HostAccessProblem, match="40-character"):
            m.deploy(invalid)
    clock["now"] += 18000
    with pytest.raises(HostAccessProblem, match="expired"):
        m.status()
    with pytest.raises(HostAccessProblem, match="expired"):
        m.deploy(sha)
    # Reauthorization requires another owner-mediated paste into Vultr.
    m.prepare(fingerprint)
    assert json.loads(before.read_text())["authorized_until"] == 0


def test_no_persistent_volume_fails_closed(monkeypatch, tmp_path, server_key):
    key, fingerprint = server_key
    m = HostAccess(str(tmp_path / "host"), host_probe=lambda: (key, fingerprint),
                   ssh_factory=FakeSSH)
    assert not m.configured()
    with pytest.raises(HostAccessProblem, match="persistent Railway volume"):
        m.prepare(fingerprint)
    with pytest.raises(HostAccessProblem, match="persistent Railway volume"):
        m.status()


def test_host_fingerprint_pin_and_private_key_permissions_are_enforced(access):
    m, _, fingerprint = access
    m.prepare(fingerprint)
    record_path = m.state_dir / "host-key.json"
    record = json.loads(record_path.read_text())
    record["host_key"] = base64.b64encode(b"unexpected").decode()
    record_path.write_text(json.dumps(record))
    with pytest.raises(HostAccessProblem, match="integrity"):
        m.status()
    record["host_key"] = m.host_probe()[0]
    record_path.write_text(json.dumps(record))
    private = m.state_dir / "identity_ed25519"
    private.chmod(0o644)
    with pytest.raises((HostAccessProblem, OSError), match="0600|permissions"):
        m.prepare(fingerprint)


def test_ssh_transport_failure_does_not_create_false_authorization(access):
    m, _, fingerprint = access
    m.prepare(fingerprint)
    FakeSSH.connect_error = OSError("network unreachable")
    with pytest.raises(OSError, match="network unreachable"):
        m.status()
    record = json.loads((m.state_dir / "host-key.json").read_text())
    assert record["authorized_until"] == 0
