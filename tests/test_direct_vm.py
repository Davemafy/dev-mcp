"""Direct Vultr gateway: no worker, durable workspace recovery, OAuth boundaries."""
from __future__ import annotations

import os
import pytest
from auth import OAuth, Settings
import vm_worker
from direct_vm import LocalMachine, DirectVmStatus


@pytest.fixture
def local(monkeypatch, tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(vm_worker, "ROOT", root)
    monkeypatch.setattr(vm_worker, "UID", os.geteuid())
    monkeypatch.setattr(vm_worker, "GID", os.getegid())
    oauth = OAuth(Settings(
        "https://devmcp.example", "p" * 26, "s" * 42,
        str(tmp_path / "auth.sqlite")))
    return LocalMachine(oauth)


def test_recover_existing_workspaces_and_execute_without_queue(local):
    root = vm_worker.ROOT
    existing = root / ("c" * 32)
    existing.mkdir(mode=0o700)
    rec = local.recover()
    assert len(rec["workspaces"]) == 1
    old = rec["workspaces"][0]["workspace_id"]
    assert local.status(old)["ready"]
    local.write(old, "src/hello.txt", "kept")
    assert local.read(old, "src/hello.txt")["content"] == "kept"
    result = local.run(old, "pwd; id -u")
    assert result["exit_code"] == 0
    assert str(existing) in result["stdout"]
    assert "backend" in local.create()
    assert len(local.recover()["workspaces"]) == 2


def test_local_backend_rejects_tampered_and_other_issuer_handles(local, tmp_path):
    handle = local.create()["workspace_id"]
    wrong = local.oauth.sign({
        "typ": "workspace", "backend": "e2b", "worker_id": "c" * 32}, 30)
    with pytest.raises(ValueError, match="another backend"):
        local.status(wrong)
    with pytest.raises(Exception):
        local.status(handle[:-4] + "ABCD")
    other = OAuth(Settings(
        "https://another.example", "p" * 26, "s" * 42,
        str(tmp_path / "other.sqlite")))
    with pytest.raises(Exception):
        LocalMachine(other).status(handle)


def test_local_command_environment_does_not_inherit_oauth_secrets(local, monkeypatch):
    monkeypatch.setenv("OWNER_PASSWORD", "never-include-this")
    monkeypatch.setenv("TOKEN_SIGNING_SECRET", "never-include-this-too")
    h = local.create()["workspace_id"]
    result = local.run(h, 'python3 -c "import os; print(os.getenv(\'OWNER_PASSWORD\', \'absent\'), os.getenv(\'TOKEN_SIGNING_SECRET\', \'absent\'))"')
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "absent absent"


def test_direct_health_status_not_fake_worker():
    result = DirectVmStatus().status()
    assert result["mode"] == "direct-vultr"
    assert result["direct_execution"] is True
    assert result["worker_connected"] is False
