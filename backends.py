"""Backend-neutral authenticated remote development worker adapter.

The gateway never executes customer commands. Remote workers must be dedicated,
isolated, unprivileged development hosts reachable through owner-managed HTTPS.
"""
from __future__ import annotations

import os
from threading import Lock
from urllib.parse import urlsplit

import httpx


class RemoteWorkerMachine:
    def __init__(self, oauth):
        self.oauth = oauth
        self.base_url = os.environ.get("WORKER_API_URL", "").rstrip("/")
        self.token = os.environ.get("WORKER_API_TOKEN", "")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or
                parsed.password or parsed.query or parsed.fragment or parsed.path):
            raise RuntimeError("WORKER_API_URL must be an HTTPS origin without a path")
        if len(self.token) < 32:
            raise RuntimeError("WORKER_API_TOKEN must be at least 32 characters")
        self.e2b_configured = False
        self.timeout = int(os.getenv("WORKER_TIMEOUT_SECONDS", "120"))
        self._client = None
        self._client_lock = Lock()

    def _http(self):
        # Reuse HTTPS connections across tool calls on a warm gateway.
        # Mutating actions are NOT retried automatically after transport failures.
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = httpx.Client(
                        timeout=min(self.timeout, 240),
                        follow_redirects=False,
                        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                    )
        return self._client

    def _call(self, action, **args):
        response = self._http().post(
            self.base_url + "/v1/execute",
            headers={"Authorization": "Bearer " + self.token},
            json={"action": action, "args": args},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Invalid worker response")
        return result

    def _workspace(self, handle):
        claims = self.oauth.verify(handle, "workspace")
        if claims.get("backend") != "worker" or not isinstance(claims.get("worker_id"), str):
            raise ValueError("Workspace handle belongs to a different backend")
        return claims["worker_id"]

    def create(self):
        result = self._call("create")
        wid = result.get("worker_id")
        if not isinstance(wid, str) or not wid:
            raise ValueError("Worker returned no workspace identifier")
        return {"workspace_id": self.oauth.sign(
            {"typ": "workspace", "backend": "worker", "worker_id": wid}, 86400),
            "home": "/home/user", "timeout_seconds": self.timeout}

    def status(self, workspace_id):
        if workspace_id == "recover":
            return {"ready": True, "workspaces": [], "message": "Remote recovery is not enabled"}
        return self._call("status", worker_id=self._workspace(workspace_id))

    def close(self, workspace_id):
        return self._call("close", worker_id=self._workspace(workspace_id))

    def write(self, workspace_id, path, content):
        if len(content.encode("utf-8")) > 500000:
            raise ValueError("File exceeds 500 KB")
        return self._call("write", worker_id=self._workspace(workspace_id), path=path, content=content)

    def read(self, workspace_id, path):
        return self._call("read", worker_id=self._workspace(workspace_id), path=path)

    def list_files(self, workspace_id, path="."):
        return self._call("list", worker_id=self._workspace(workspace_id), path=path)

    def run(self, workspace_id, command, timeout_seconds=120):
        return self._call("run", worker_id=self._workspace(workspace_id),
                          command=command, timeout_seconds=min(180, max(1, timeout_seconds)))

    def start_process(self, workspace_id, command):
        return self._call("start", worker_id=self._workspace(workspace_id), command=command)

    def list_processes(self, workspace_id):
        return self._call("processes", worker_id=self._workspace(workspace_id))

    def stop_process(self, workspace_id, pid):
        return self._call("stop", worker_id=self._workspace(workspace_id), pid=int(pid))

    def preview_url(self, workspace_id, port):
        return self._call("preview", worker_id=self._workspace(workspace_id), port=int(port))


def build_machine(oauth, sandbox_factory=None):
    provider = os.getenv("WORKSPACE_BACKEND", "e2b").strip().lower()
    if provider == "e2b":
        from machine import Machine
        return Machine(oauth, sandbox_factory=sandbox_factory)
    if provider == "local":
        if sandbox_factory is not None:
            raise ValueError("Local Vultr backend cannot use an E2B sandbox factory")
        from direct_vm import LocalMachine
        return LocalMachine(oauth)
    if provider == "worker":
        if sandbox_factory is not None:
            raise ValueError("Cannot override a worker backend with an E2B factory")
        return RemoteWorkerMachine(oauth)
    raise RuntimeError("Unsupported WORKSPACE_BACKEND: " + provider)
