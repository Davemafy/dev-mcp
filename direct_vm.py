"""Direct, single-owner development execution on the Vultr VM.

No polling worker, Railway gateway or separate execution queue. The FastMCP
gateway and workspace executor share one deployment while development commands
still run as an unprivileged Unix account with a sanitized environment.

The existing /srv/devmcp-workspaces directory is mounted at /workspaces.
Recover re-signs existing workspace IDs under the NEW OAuth issuer; previous
Railway workspace handles do not remain valid on the new endpoint.
"""
from __future__ import annotations

from vm_worker import execute
from host_admin import admin_status, run_host_command


class LocalMachine:
    e2b_configured = False

    def __init__(self, oauth):
        self.oauth = oauth

    def _workspace(self, handle):
        data = self.oauth.verify(handle, "workspace")
        if data.get("backend") != "local":
            raise ValueError("Workspace belongs to another backend")
        wid = data.get("worker_id")
        # vm_worker validates actual directory and IDs independently.
        if not isinstance(wid, str):
            raise ValueError("Invalid local workspace handle")
        return wid

    def _sign(self, worker_id):
        return self.oauth.sign(
            {"typ": "workspace", "backend": "local", "worker_id": worker_id},
            30 * 86400,
        )

    def create(self):
        wid = execute("create", {})["worker_id"]
        return {"workspace_id": self._sign(wid), "home": "/home/user",
                "persistent": True, "backend": "local"}

    def recover(self):
        existing = execute("recover", {})["workspaces"]
        return {"workspaces": [{"workspace_id": self._sign(wid),
                                "home": "/home/user"} for wid in existing],
                "persistent": True, "backend": "local"}

    def status(self, handle):
        if handle == "recover":
            return self.recover()
        return execute("status", {"worker_id": self._workspace(handle)})

    def close(self, handle):
        return execute("close", {"worker_id": self._workspace(handle)})

    def write(self, handle, path, content):
        return execute("write", {"worker_id": self._workspace(handle),
                                 "path": path, "content": content})

    def read(self, handle, path):
        return execute("read", {"worker_id": self._workspace(handle), "path": path})

    def list_files(self, handle, path="."):
        return execute("list", {"worker_id": self._workspace(handle), "path": path})

    def run(self, handle, command, timeout_seconds=90):
        if not isinstance(command, str) or not 1 <= len(command) <= 10000:
            raise ValueError("Invalid command")
        return execute("run", {"worker_id": self._workspace(handle),
                               "command": command,
                               "timeout_seconds": min(max(int(timeout_seconds), 1), 600)})

    def start_process(self, handle, command):
        return execute("start", {"worker_id": self._workspace(handle),
                                 "command": command})

    def list_processes(self, handle):
        return execute("processes", {"worker_id": self._workspace(handle)})

    def stop_process(self, handle, pid):
        return execute("stop", {"worker_id": self._workspace(handle), "pid": int(pid)})

    def artifact_read(self, handle, path, offset=0, chunk_size=24000):
        return execute("artifact_read", {
            "worker_id": self._workspace(handle), "path": path,
            "offset": offset, "chunk_size": chunk_size,
        })

    def preview_url(self, handle, port):
        self._workspace(handle)
        return {"available": False,
                "message": "Vultr workspace previews require a separate authenticated reverse proxy; no public preview has been exposed."}


class DirectVmStatus:
    def status(self):
        return {"configured": True, "mode": "direct-vultr",
                "persistent": True, "worker_connected": False,
                "direct_execution": True, "unfinished_tasks": 0}


def register_direct_vm_tools(mcp, oauth):
    """Expose the existing VM tool names without Railway or a polling daemon."""
    machine = LocalMachine(oauth)
    state = DirectVmStatus()

    @mcp.tool()
    def vm_status() -> dict:
        """Inspect the on-VM direct executor; there is no remote polling worker."""
        return state.status()

    @mcp.tool()
    def vm_workspace_create() -> dict:
        """Create a persistent local Vultr workspace."""
        return machine.create()

    @mcp.tool()
    def vm_workspace_recover() -> dict:
        """Recover all prior Vultr workspace folders and issue new signed handles."""
        return machine.recover()

    @mcp.tool()
    def vm_workspace_status(workspace_id: str) -> dict:
        """Inspect an authenticated local Vultr workspace."""
        return machine.status(workspace_id)

    @mcp.tool()
    def vm_workspace_close(workspace_id: str) -> dict:
        """Delete only the specified workspace, not other VM files."""
        return machine.close(workspace_id)

    @mcp.tool()
    def vm_run(workspace_id: str, command: str, timeout_seconds: int = 90) -> dict:
        """Run an unprivileged development command directly on Vultr."""
        return machine.run(workspace_id, command, timeout_seconds)

    @mcp.tool()
    def vm_write_file(workspace_id: str, path: str, content: str) -> dict:
        """Write UTF-8 data under the specified workspace."""
        return machine.write(workspace_id, path, content)

    @mcp.tool()
    def vm_read_file(workspace_id: str, path: str) -> dict:
        """Read UTF-8 data inside the specified workspace."""
        return machine.read(workspace_id, path)

    @mcp.tool()
    def vm_list_files(workspace_id: str, path: str = ".") -> dict:
        """List files only within the specified workspace."""
        return machine.list_files(workspace_id, path)

    @mcp.tool()
    def vm_artifact_read(workspace_id: str, path: str,
                         offset: int = 0, chunk_size: int = 24000) -> dict:
        """Read a bounded binary artifact chunk from an authorized workspace."""
        return machine.artifact_read(workspace_id, path, offset, chunk_size)

    @mcp.tool()
    def vm_start_process(workspace_id: str, command: str) -> dict:
        """Launch a bounded background process directly on Vultr."""
        return machine.start_process(workspace_id, command)

    @mcp.tool()
    def vm_list_processes(workspace_id: str) -> dict:
        """List development processes in the specified workspace."""
        return machine.list_processes(workspace_id)

    @mcp.tool()
    def vm_stop_process(workspace_id: str, pid: int) -> dict:
        """Stop an owned workspace process by PID."""
        return machine.stop_process(workspace_id, pid)

    @mcp.tool()
    def vm_host_admin_status() -> dict:
        """Verify owner-authorized SSH administration of the Vultr host."""
        return admin_status()

    @mcp.tool()
    def vm_host_admin_run(command: str, timeout_seconds: int = 90) -> dict:
        """Run an owner-authenticated root-equivalent host command via pinned SSH."""
        return run_host_command(command, timeout_seconds)

    return [], state
