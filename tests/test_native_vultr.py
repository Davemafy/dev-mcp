"""Native Ubuntu service tests: no Docker, preserves old UID-owned workspaces."""
from __future__ import annotations
import os
from pathlib import Path
import pytest
import vm_worker
import host_admin
from auth import OAuth, Settings
from direct_vm import LocalMachine


def test_native_unprivileged_workspace_create_and_write(monkeypatch, tmp_path):
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    monkeypatch.setattr(vm_worker, "ROOT", workspace)
    monkeypatch.setattr(vm_worker, "UID", os.geteuid())
    monkeypatch.setattr(vm_worker, "GID", os.getegid())
    # When the gateway is not root, no chown call must be attempted.
    if os.geteuid() != 0:
        def forbidden_chown(*args):
            raise AssertionError("Non-root path must not chown")
        monkeypatch.setattr(vm_worker.os, "chown", forbidden_chown)
    auth = OAuth(Settings("https://devmcp.example", "p"*24, "s"*42,
                          str(tmp_path / "auth.sqlite")))
    local = LocalMachine(auth)
    h = local.create()["workspace_id"]
    local.write(h, "sub/note.txt", "native")
    assert local.read(h, "sub/note.txt")["content"] == "native"
    assert len(local.recover()["workspaces"]) == 1


def test_native_ssh_configuration_is_environment_driven(monkeypatch, tmp_path):
    state = tmp_path / "identity"
    state.mkdir()
    key = state / "identity_ed25519"
    known = state / "known_hosts"
    key.write_text("mock key")
    key.chmod(0o600)
    known.write_text("127.0.0.1 ssh-ed25519 AAAA\n")
    known.chmod(0o600)
    monkeypatch.setattr(host_admin, "STATE", state)
    monkeypatch.setattr(host_admin, "HOST", "127.0.0.1")
    assert host_admin._ready()
    known.write_text("host.docker.internal ssh-ed25519 AAAA\n")
    assert not host_admin._ready()


def test_native_systemd_binds_only_loopback():
    service = Path(__file__).resolve().parents[1] / "deploy" / "devmcp-native.service"
    data = service.read_text()
    assert "BIND_HOST=127.0.0.1" in data
    assert "User=devmcp" in data
    assert "DEVMCP_WORKSPACE_ROOT=/srv/devmcp-workspaces" in data
    assert "ExecStart=/opt/devmcp-python/bin/python" in data
    assert "Docker" not in data
