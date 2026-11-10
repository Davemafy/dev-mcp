import os
from types import SimpleNamespace
import pytest
from backends import build_machine, RemoteWorkerMachine
from auth import Settings, OAuth


@pytest.fixture
def oauth(tmp_path):
    return OAuth(Settings("https://devmcp.example", "x" * 24, "y" * 40, str(tmp_path / "oauth.db")))


def test_provider_selection(monkeypatch, oauth):
    monkeypatch.setenv("WORKSPACE_BACKEND", "unknown")
    with pytest.raises(RuntimeError, match="Unsupported"):
        build_machine(oauth)
    monkeypatch.setenv("WORKSPACE_BACKEND", "e2b")
    assert build_machine(oauth, sandbox_factory=SimpleNamespace()).__class__.__name__ == "Machine"


def test_worker_requires_https_and_strong_secret(monkeypatch, oauth):
    monkeypatch.setenv("WORKSPACE_BACKEND", "worker")
    monkeypatch.setenv("WORKER_API_URL", "http://worker.example")
    monkeypatch.setenv("WORKER_API_TOKEN", "x" * 40)
    with pytest.raises(RuntimeError, match="HTTPS"):
        build_machine(oauth)
    monkeypatch.setenv("WORKER_API_URL", "https://worker.example")
    monkeypatch.setenv("WORKER_API_TOKEN", "bad")
    with pytest.raises(RuntimeError, match="32 characters"):
        build_machine(oauth)


def test_remote_machine_scopes_handles(monkeypatch, oauth):
    monkeypatch.setenv("WORKSPACE_BACKEND", "worker")
    monkeypatch.setenv("WORKER_API_URL", "https://worker.example")
    monkeypatch.setenv("WORKER_API_TOKEN", "x" * 40)
    worker = build_machine(oauth)
    monkeypatch.setattr(worker, "_call", lambda action, **kwargs: (
        {"worker_id": "id-1"} if action == "create" else {"action": action, **kwargs}
    ))
    handle = worker.create()["workspace_id"]
    assert worker.status(handle)["worker_id"] == "id-1"
    assert worker.run(handle, "echo ok")["action"] == "run"
    with pytest.raises(ValueError, match="different backend"):
        worker.status(oauth.sign({"typ": "workspace", "sandbox_id": "e2b"}, 120))


def test_remote_disallows_redirects(monkeypatch, oauth):
    monkeypatch.setenv("WORKER_API_URL", "https://worker.example")
    monkeypatch.setenv("WORKER_API_TOKEN", "x" * 40)
    worker = RemoteWorkerMachine(oauth)
    captured = {}
    class MockClient:
        def __init__(self, **kwargs): captured.update(kwargs)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, headers, json):
            assert url == "https://worker.example/v1/execute"
            assert headers["Authorization"].startswith("Bearer ")
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"worker_id":"id-2"})
    monkeypatch.setattr("backends.httpx.Client", MockClient)
    worker.create()
    assert captured["follow_redirects"] is False


def test_serverless_requires_durable_auth(monkeypatch):
    monkeypatch.setenv("STATELESS_GATEWAY", "1")
    monkeypatch.setenv("OWNER_PASSWORD", "p" * 24)
    monkeypatch.setenv("TOKEN_SIGNING_SECRET", "s" * 40)
    monkeypatch.delenv("AUTH_REDIS_URL", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://devmcp.example")
    with pytest.raises(RuntimeError, match="Shared Redis"):
        Settings.from_env()


def test_https_worker_reuses_connection_pool(monkeypatch, oauth):
    monkeypatch.setenv("WORKER_API_URL", "https://worker.example")
    monkeypatch.setenv("WORKER_API_TOKEN", "x" * 40)
    created = []
    class FakeClient:
        def __init__(self, **kwargs):
            created.append(kwargs)
        def post(self, url, headers, json):
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"worker_id": "worker-ok"})
    monkeypatch.setattr("backends.httpx.Client", FakeClient)
    worker = RemoteWorkerMachine(oauth)
    worker.create()
    worker.create()
    assert len(created) == 1
    assert created[0]["follow_redirects"] is False
