"""Owner-authorized VM host SSH tests. Never open real SSH connections."""
from __future__ import annotations
import os
import stat
from types import SimpleNamespace
import pytest
import host_admin
import vm_worker


def test_admin_unconfigured_is_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(host_admin, "STATE", tmp_path / "missing")
    assert host_admin.admin_status()["configured"] is False
    with pytest.raises(ValueError, match="not configured"):
        host_admin.run_host_command("id")
    assert vm_worker.execute("host_admin_status", {})["configured"] is False


def test_ssh_pins_host_key_and_does_not_export_key(monkeypatch, tmp_path):
    state = tmp_path / "admin"
    state.mkdir(mode=0o700)
    key = state / "identity_ed25519"
    known = state / "known_hosts"
    key.write_text("mock private key")
    key.chmod(0o600)
    known.write_text("host.docker.internal ssh-ed25519 AAAA-mock\n")
    known.chmod(0o600)
    monkeypatch.setattr(host_admin, "STATE", state)
    monkeypatch.setattr(host_admin.os, "geteuid", lambda: 0)
    # In CI files are created by the current (non-root) user. Simulate UID 0
    # for the security-property unit test and never invoke real SSH.
    old_ready = host_admin._ready
    def fake_ready():
        return True
    monkeypatch.setattr(host_admin, "_ready", fake_ready)
    captured = {}
    class FakeProc:
        pid = 2121
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            kwargs["stdout"].write(b"0\n")
            kwargs["stderr"].write(b"")
        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(host_admin.subprocess, "Popen", FakeProc)
    result = host_admin.run_host_command("id -u")
    assert result["exit_code"] == 0
    assert result["stdout"] == "0\n"
    assert "StrictHostKeyChecking=yes" in captured["argv"]
    assert "BatchMode=yes" in captured["argv"]
    assert "ClearAllForwardings=yes" in captured["argv"]
    assert captured["argv"][-1] == "'id -u'"
    assert "mock private key" not in str(result)
    with pytest.raises(ValueError, match="Invalid host command"):
        host_admin.run_host_command("")


def test_worker_dispatch_and_admin_file_permissions(monkeypatch, tmp_path):
    state = tmp_path / "admin"
    state.mkdir()
    (state / "identity_ed25519").write_text("private")
    (state / "identity_ed25519").chmod(0o644)
    (state / "known_hosts").write_text("host.docker.internal ssh-ed25519 mock\n")
    monkeypatch.setattr(host_admin, "STATE", state)
    assert host_admin._ready() is False
    monkeypatch.setattr(vm_worker, "run_host_command", lambda cmd, timeout: {"command":cmd,"timeout":timeout})
    out = vm_worker.execute("host_admin_run", {"command":"echo ok","timeout_seconds":20})
    assert out == {"command":"echo ok","timeout":20}
