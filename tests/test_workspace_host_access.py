"""Offline tests for E2B-network fallback to restricted Refit SSH."""
import json
import pytest

from host_access import HostAccessProblem
from workspace_host_access import prepare, status, deploy

FINGERPRINT = "SHA256:" + "A" * 43
PUBLIC = "ssh-ed25519 " + "A" * 90 + " refit-devmcp-gateway"

class FakeMachine:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "exit_code": 0,
            "stdout": json.dumps({
                "client_public_key": PUBLIC, "host_fingerprint": FINGERPRINT
            }),
            "stderr": "",
        }

    def run(self, workspace_id, command, timeout_seconds):
        self.calls.append((workspace_id, command, timeout_seconds))
        return self.result


def test_prepare_requires_fingerprint_and_returns_only_public_key():
    machine = FakeMachine()
    with pytest.raises(HostAccessProblem, match="fingerprint"):
        prepare(machine, "signed-workspace", "invalid")
    assert machine.calls == []
    result = prepare(machine, "signed-workspace", FINGERPRINT)
    assert result["host"] == "80.240.30.92"
    assert result["client_public_key"] == PUBLIC
    assert result["authorized"] is False
    assert result["persistent"] is False
    assert "setup-devmcp-host-access.sh" in result["host_setup"]
    assert "PRIVATE KEY" not in json.dumps(result)
    assert machine.calls[0][0] == "signed-workspace"
    assert "ssh-keyscan" in machine.calls[0][1]


def test_prepare_fails_closed_on_mismatched_result_or_script_failure():
    machine = FakeMachine({"exit_code": 0, "stdout": json.dumps({
        "client_public_key": PUBLIC, "host_fingerprint": "SHA256:" + "B" * 43
    }), "stderr": ""})
    with pytest.raises(HostAccessProblem, match="Invalid workspace SSH key response"):
        prepare(machine, "signed-workspace", FINGERPRINT)

    failed = FakeMachine({"exit_code": 1, "stdout": "", "stderr": "host fingerprint differs"})
    with pytest.raises(HostAccessProblem, match="fingerprint differs"):
        prepare(failed, "signed-workspace", FINGERPRINT)


def test_status_is_fixed_to_pinned_host_with_strict_known_hosts():
    machine = FakeMachine({"exit_code": 0, "stdout": "Service: active", "stderr": ""})
    result = status(machine, "signed-workspace")
    assert result["authorized"] is True
    assert result["host"] == "80.240.30.92"
    _, command, timeout = machine.calls[0]
    assert "StrictHostKeyChecking=yes" in command
    assert "PasswordAuthentication=no" in command
    assert "UserKnownHostsFile=" in command
    assert "ClearAllForwardings=yes" in command
    assert "root@80.240.30.92 status" in command
    assert timeout == 30

    machine = FakeMachine({"exit_code": 255, "stdout": "", "stderr": "Permission denied"})
    assert status(machine, "signed-workspace")["authorized"] is False


def test_deploy_requires_exact_sha_and_has_no_shell_injection():
    machine = FakeMachine({"exit_code": 0, "stdout": "ok", "stderr": ""})
    for bad in ["main", "a" * 39, "A" * 40, "a" * 40 + ";rm -rf /"]:
        with pytest.raises(HostAccessProblem, match="40-character"):
            deploy(machine, "signed-workspace", bad)
    assert machine.calls == []

    result = deploy(machine, "signed-workspace", "a" * 40)
    assert result["exit_code"] == 0
    assert machine.calls[0][1].endswith("'deploy " + "a" * 40 + "'")
    assert machine.calls[0][2] == 300
