"""Durable, single-owner, outbound-only VM executor transport.

Railway owns the SQLite queue on its mounted volume. The VM initiates all
connections over HTTPS; no SSH port, public worker API, or VM root credentials
are required. An execution receipt is never retried automatically.
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from starlette.responses import JSONResponse
from starlette.routing import Route

_ACTIONS = frozenset({
    "create", "recover", "status", "close", "read", "write", "list",
    "run", "start", "processes", "stop",
})
_VM_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_ARGS = 550_000
MAX_RESULT = 100_000


class VmLink:
    def __init__(self, oauth, *, test_db=None):
        self.oauth = oauth
        self.token = os.getenv("VM_WORKER_TOKEN", "")
        db = test_db or os.getenv("VM_JOB_DB", "/data/devmcp-vm-jobs.sqlite")
        self.db_path = Path(db)
        durable_path = (
            self.db_path.is_absolute()
            and str(self.db_path).startswith("/data/")
            and ".." not in self.db_path.parts
            and os.path.ismount("/data")
        )
        self.enabled = (
            len(self.token) >= 32 and os.getenv("STATELESS_GATEWAY") != "1"
            and (durable_path or test_db is not None)
        )
        if self.enabled:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._db() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS vm_tasks(
                        id TEXT PRIMARY KEY,
                        action TEXT NOT NULL,
                        args TEXT NOT NULL,
                        state TEXT NOT NULL,
                        result TEXT,
                        created REAL NOT NULL,
                        claimed REAL,
                        finished REAL
                    );
                    CREATE INDEX IF NOT EXISTS vm_tasks_queue
                        ON vm_tasks(state,created);
                    CREATE TABLE IF NOT EXISTS vm_heartbeat(
                        worker TEXT PRIMARY KEY, seen REAL NOT NULL
                    );
                """)

    def _db(self):
        connection = sqlite3.connect(str(self.db_path), timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _require(self):
        if not self.enabled:
            raise RuntimeError(
                "VM link disabled: set VM_WORKER_TOKEN (32+ chars) and mount /data on Railway."
            )

    def status(self):
        result = {"configured": self.enabled, "mode": "outbound-https-polling",
                  "persistent": self.enabled, "worker_connected": False}
        if self.enabled:
            with self._db() as connection:
                row = connection.execute(
                    "SELECT MAX(seen) FROM vm_heartbeat"
                ).fetchone()
                pending = connection.execute(
                    "SELECT COUNT(*) FROM vm_tasks WHERE state IN ('queued','running')"
                ).fetchone()[0]
            age = None if row[0] is None else max(0, round(time.time() - row[0], 1))
            result.update({"worker_connected": age is not None and age < 20,
                           "heartbeat_age_seconds": age, "unfinished_tasks": pending})
        return result

    def _workspace(self, signed: str):
        data = self.oauth.verify(signed, "workspace")
        if data.get("backend") != "vm" or not isinstance(data.get("worker_id"), str):
            raise ValueError("Not a VM workspace handle")
        if not _VM_ID.fullmatch(data["worker_id"]):
            raise ValueError("Invalid VM workspace identifier")
        return data["worker_id"]

    def _sign_workspace(self, worker_id):
        if not isinstance(worker_id, str) or not _VM_ID.fullmatch(worker_id):
            raise ValueError("VM worker returned an invalid workspace")
        return self.oauth.sign(
            {"typ": "workspace", "backend": "vm", "worker_id": worker_id},
            30 * 86400,
        )

    def _render(self, action, data):
        if not isinstance(data, dict):
            raise ValueError("Invalid VM receipt")
        if data.get("error"):
            return data
        if action == "create" and data.get("worker_id"):
            return {"workspace_id": self._sign_workspace(data["worker_id"]),
                    "home": "/home/user", "persistent": True}
        if action == "recover" and isinstance(data.get("workspaces"), list):
            return {"workspaces": [
                {"workspace_id": self._sign_workspace(wid), "home": "/home/user"}
                for wid in data["workspaces"][:100]
            ], "persistent": True}
        return data

    def submit(self, action, args=None, wait_seconds=12):
        self._require()
        if action not in _ACTIONS or not isinstance(args or {}, dict):
            raise ValueError("Unsupported VM operation")
        if not self.status()["worker_connected"]:
            return {"queued": False, "error": "VM worker offline; no command was queued"}
        args = args or {}
        payload = json.dumps(args, separators=(",", ":"))
        if len(payload.encode()) > MAX_ARGS:
            raise ValueError("VM operation payload exceeds limit")
        job_id = secrets.token_hex(16)
        with self._db() as conn:
            backlog = conn.execute(
                "SELECT COUNT(*) FROM vm_tasks WHERE state IN ('queued','running')"
            ).fetchone()[0]
            if backlog >= 64:
                raise RuntimeError("VM queue is full; drain outstanding operations first")
            conn.execute(
                "INSERT INTO vm_tasks(id,action,args,state,created) VALUES(?,?,?,'queued',?)",
                (job_id, action, payload, time.time()),
            )
        deadline = time.monotonic() + max(0, min(float(wait_seconds), 20))
        while time.monotonic() < deadline:
            state = self.job(job_id)
            if state["state"] in ("finished", "failed"):
                return state["result"]
            time.sleep(0.2)
        return {"pending": True, "job_id": job_id,
                "message": "Use vm_job_status to obtain the receipt; do not re-run the command."}

    def job(self, job_id):
        self._require()
        if not isinstance(job_id, str) or not _VM_ID.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        with self._db() as conn:
            row = conn.execute(
                "SELECT action,state,result,created,claimed FROM vm_tasks WHERE id=?",
                (job_id,),
            ).fetchone()
        if not row:
            return {"error": "VM task not found", "job_id": job_id}
        action, state, raw, created, claimed = row
        result = self._render(action, json.loads(raw)) if raw else None
        if state == "running" and claimed and time.time()-claimed > 300:
            return {"job_id": job_id, "state": "unknown",
                    "message": "Execution result unconfirmed; no automatic retry."}
        return {"job_id": job_id, "state": state, "result": result}

    def cancel(self, job_id):
        self._require()
        if not isinstance(job_id, str) or not _VM_ID.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        with self._db() as connection:
            cursor = connection.execute(
                "UPDATE vm_tasks SET state='cancelled',finished=? "
                "WHERE id=? AND state='queued'",
                (time.time(), job_id),
            )
        return {"cancelled": cursor.rowcount == 1,
                "message": "Running tasks cannot be safely cancelled by this queue."}

    def command(self, action, signed=None, **kwargs):
        if signed is not None:
            kwargs["worker_id"] = self._workspace(signed)
        return self.submit(action, kwargs)

    def claim(self):
        self._require()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id,action,args FROM vm_tasks WHERE state='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE vm_tasks SET state='running',claimed=? WHERE id=?",
                    (time.time(), row[0]),
                )
        if not row:
            return {"task": None}
        return {"task": {"id": row[0], "action": row[1], "args": json.loads(row[2])}}

    def report(self, data):
        self._require()
        task_id = data.get("id")
        result = data.get("result")
        if not isinstance(task_id, str) or not _VM_ID.fullmatch(task_id):
            raise ValueError("Invalid task ID")
        if not isinstance(result, dict) or len(json.dumps(result).encode()) > MAX_RESULT:
            raise ValueError("Invalid or excessive task receipt")
        with self._db() as conn:
            cursor = conn.execute(
                "UPDATE vm_tasks SET state=?,result=?,finished=? WHERE id=? AND state='running'",
                ("failed" if result.get("error") else "finished",
                 json.dumps(result, separators=(",", ":")), time.time(), task_id),
            )
        return {"accepted": cursor.rowcount == 1}

    async def worker_api(self, request):
        if not self.enabled:
            return JSONResponse({"error": "not_configured"}, status_code=503)
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, "Bearer " + self.token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if int(request.headers.get("content-length", "0")) > 120_000:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            if request.url.path.endswith("/claim"):
                with self._db() as conn:
                    conn.execute(
                        "INSERT INTO vm_heartbeat(worker,seen) VALUES('primary',?) "
                        "ON CONFLICT(worker) DO UPDATE SET seen=excluded.seen",
                        (time.time(),),
                    )
                value = self.claim()
            elif request.url.path.endswith("/report"):
                data = await request.json()
                if not isinstance(data, dict):
                    raise ValueError("Bad report")
                value = self.report(data)
            else:
                value = {"error": "invalid_endpoint"}
            return JSONResponse(value)
        except (ValueError, TypeError, sqlite3.DatabaseError) as exc:
            return JSONResponse({"error": str(exc)[:160]}, status_code=400)


def register_vm_tools(mcp, oauth):
    """Register an independent VM toolset; do not disrupt existing E2B handles."""
    link = VmLink(oauth)

    @mcp.tool()
    def vm_status() -> dict:
        """Check persistent Vultr development worker presence and queue health."""
        return link.status()

    @mcp.tool()
    def vm_workspace_create() -> dict:
        """Create a persistent workspace inside the isolated Vultr development container."""
        return link.submit("create")

    @mcp.tool()
    def vm_workspace_recover() -> dict:
        """List surviving VM workspaces and issue new signed access handles."""
        return link.submit("recover")

    @mcp.tool()
    def vm_workspace_status(workspace_id: str) -> dict:
        """Check a signed persistent VM workspace."""
        return link.command("status", workspace_id)

    @mcp.tool()
    def vm_workspace_close(workspace_id: str) -> dict:
        """Delete an explicitly selected persistent VM workspace and its contents."""
        return link.command("close", workspace_id)

    @mcp.tool()
    def vm_run(workspace_id: str, command: str, timeout_seconds: int = 90) -> dict:
        """Run an authorized development command in the isolated VM workspace, not the VM host."""
        if not isinstance(command, str) or not 1 <= len(command) <= 10000:
            raise ValueError("Invalid command")
        return link.command("run", workspace_id, command=command,
                            timeout_seconds=min(max(1, timeout_seconds), 180))

    @mcp.tool()
    def vm_write_file(workspace_id: str, path: str, content: str) -> dict:
        """Write UTF-8 text under an isolated VM workspace."""
        return link.command("write", workspace_id, path=path, content=content)

    @mcp.tool()
    def vm_read_file(workspace_id: str, path: str) -> dict:
        """Read a bounded UTF-8 file from an isolated VM workspace."""
        return link.command("read", workspace_id, path=path)

    @mcp.tool()
    def vm_list_files(workspace_id: str, path: str = ".") -> dict:
        """List files without leaving the isolated VM workspace."""
        return link.command("list", workspace_id, path=path)

    @mcp.tool()
    def vm_start_process(workspace_id: str, command: str) -> dict:
        """Start a bounded development process in the VM's isolated container."""
        return link.command("start", workspace_id, command=command)

    @mcp.tool()
    def vm_list_processes(workspace_id: str) -> dict:
        """List tracked development processes."""
        return link.command("processes", workspace_id)

    @mcp.tool()
    def vm_stop_process(workspace_id: str, pid: int) -> dict:
        """Stop a tracked process inside the isolated VM worker."""
        return link.command("stop", workspace_id, pid=pid)

    @mcp.tool()
    def vm_job_status(job_id: str) -> dict:
        """Read a queued VM operation receipt without repeating side effects."""
        return link.job(job_id)

    @mcp.tool()
    def vm_job_cancel(job_id: str) -> dict:
        """Cancel a VM operation that has not yet been claimed; never silently interrupt running work."""
        return link.cancel(job_id)

    return [
        Route("/internal/vm/claim", link.worker_api, methods=["POST"]),
        Route("/internal/vm/report", link.worker_api, methods=["POST"]),
    ], link
