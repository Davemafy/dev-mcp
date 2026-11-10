"""Durable VM dispatch and execution-boundary tests (no real VM required)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import httpx
import pytest

import vm_link
import vm_worker


class OAuthStub:
    def sign(self, data, ttl):
        return json.dumps(data)
    def verify(self, token, typ):
        data = json.loads(token)
        assert data["typ"] == typ
        return data


@pytest.fixture
def link(monkeypatch, tmp_path):
    monkeypatch.setenv("VM_WORKER_TOKEN", "v" * 40)
    monkeypatch.delenv("STATELESS_GATEWAY", raising=False)
    worker = vm_link.VmLink(OAuthStub(), test_db=str(tmp_path / "tasks.sqlite"))
    import time
    with worker._db() as conn:
        conn.execute("INSERT INTO vm_heartbeat(worker,seen) VALUES('primary',?)",
                     (time.time(),))
    return worker


def test_unconfigured_worker_remains_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("VM_WORKER_TOKEN", raising=False)
    inst = vm_link.VmLink(OAuthStub(), test_db=str(tmp_path / "tasks.sqlite"))
    assert not inst.status()["configured"]
    with pytest.raises(RuntimeError, match="disabled"):
        inst.submit("create")


def test_queue_claim_once_and_sign_workspace(link):
    pending = link.submit("create", wait_seconds=0)
    task = link.claim()["task"]
    assert task["id"] == pending["job_id"] and task["action"] == "create"
    assert link.claim()["task"] is None  # exactly once, including gateway restart
    worker_id = "a" * 32
    assert link.report({"id": task["id"], "result": {"worker_id": worker_id}})["accepted"]
    assert not link.report({"id": task["id"], "result": {"worker_id": worker_id}})["accepted"]
    receipt = link.job(task["id"])
    assert receipt["state"] == "finished"
    signed = receipt["result"]["workspace_id"]
    assert link._workspace(signed) == worker_id
    with pytest.raises(ValueError, match="Not a VM"):
        link._workspace(json.dumps({"typ":"workspace","backend":"e2b","worker_id":worker_id}))


def test_recover_and_failure_receipts(link):
    job = link.submit("recover", wait_seconds=0)["job_id"]
    task = link.claim()["task"]
    assert link.report({"id":task["id"],"result":{"workspaces":["b"*32]}})["accepted"]
    assert link._workspace(link.job(job)["result"]["workspaces"][0]["workspace_id"]) == "b"*32
    job = link.submit("run", {"worker_id":"a"*32,"command":"false"}, wait_seconds=0)["job_id"]
    task = link.claim()["task"]
    link.report({"id":task["id"],"result":{"error":"Timeout"}})
    assert link.job(job)["state"] == "failed"


def test_queue_status_and_worker_endpoint_auth(link):
    from starlette.applications import Starlette
    from starlette.routing import Route
    app = Starlette(routes=[
        Route("/internal/vm/claim", link.worker_api, methods=["POST"]),
        Route("/internal/vm/report", link.worker_api, methods=["POST"]),
    ])
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://gateway.example"
        ) as client:
            unauthorized = await client.post("/internal/vm/claim", json={})
            assert unauthorized.status_code == 401
            authorized = await client.post(
                "/internal/vm/claim", json={},
                headers={"Authorization": "Bearer " + "v"*40}
            )
            assert authorized.status_code == 200
            assert authorized.json()["task"] is None
    asyncio.run(run())
    assert link.status()["worker_connected"]


def test_worker_rejects_escape_and_keeps_control_token_private(monkeypatch, tmp_path):
    monkeypatch.setattr(vm_worker, "ROOT", tmp_path)
    root = tmp_path / ("a"*32)
    root.mkdir()
    assert vm_worker._file(root, "folder/test.txt") == root / "folder/test.txt"
    with pytest.raises(ValueError, match="escapes"):
        vm_worker._file(root, "../escape.txt")
    with pytest.raises(ValueError, match="Absolute"):
        vm_worker._file(root, "/etc/passwd")
    (root / "link").symlink_to("/etc")
    with pytest.raises(ValueError, match="escapes"):
        vm_worker._file(root, "link/passwd")

    monkeypatch.setattr(vm_worker, "_drop_privileges", lambda: None)
    monkeypatch.setenv("VM_WORKER_TOKEN", "secret-not-for-child")
    proc = vm_worker.run_command(root, "python3 -c 'import os;print(os.getenv(\"VM_WORKER_TOKEN\",\"not-present\"))'")
    assert proc["exit_code"] == 0
    assert proc["stdout"].strip() == "not-present"


def test_worker_workspace_ids_and_overlong_command(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_worker, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        vm_worker._root("../etc")
    with pytest.raises(ValueError):
        vm_worker._command(tmp_path, "x"*10001)

def test_queue_refuses_offline_commands_and_allows_cancel(link):
    import time
    with link._db() as conn:
        conn.execute("DELETE FROM vm_heartbeat")
    assert link.submit("run", {"command": "echo hi"}, wait_seconds=0)["queued"] is False
    with link._db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM vm_tasks").fetchone()[0]
        conn.execute("INSERT INTO vm_heartbeat(worker,seen) VALUES('primary',?)",
                     (time.time(),))
    assert count == 0
    job = link.submit("run", {"command": "echo hi"}, wait_seconds=0)["job_id"]
    assert link.cancel(job)["cancelled"] is True
    assert link.claim()["task"] is None
    assert link.job(job)["state"] == "cancelled"
