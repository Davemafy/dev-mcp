"""dev-mcp: Python FastMCP gateway on Railway, persistent E2B development workspace."""
from __future__ import annotations

import os
from auth import Settings, OAuth, BearerGate
from machine import Machine


def build_app(machine_factory=None):
    from fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route, Mount

    settings=Settings.from_env()
    oauth=OAuth(settings)
    machine=Machine(oauth,sandbox_factory=machine_factory)
    mcp=FastMCP('dev-mcp · E2B cloud machine')

    @mcp.tool()
    def workspace_create() -> dict:
        """Create a fresh disposable Linux workspace in E2B; pass its workspace_id to all subsequent tools."""
        return machine.create()

    @mcp.tool()
    def workspace_status(workspace_id: str) -> dict:
        """Check if an existing E2B workspace remains alive."""
        return machine.status(workspace_id)

    @mcp.tool()
    def workspace_close(workspace_id: str) -> dict:
        """Permanently terminate an E2B machine and discard its filesystem."""
        return machine.close(workspace_id)

    @mcp.tool()
    def write_file(workspace_id: str, path: str, content: str) -> dict:
        """Write UTF-8 text in the existing workspace; paths are relative to /home/user."""
        return machine.write(workspace_id,path,content)

    @mcp.tool()
    def read_file(workspace_id: str, path: str) -> dict:
        """Read a text file in the same E2B workspace; large output is truncated."""
        return machine.read(workspace_id,path)

    @mcp.tool()
    def list_files(workspace_id: str, path: str='.') -> dict:
        """List up to 100 files in /home/user or its subdirectories."""
        return machine.list_files(workspace_id,path)

    @mcp.tool()
    def run_terminal_command(workspace_id: str, command: str, timeout_seconds: int=120) -> dict:
        """Execute a shell command in the existing E2B Linux machine, preserving stdout/stderr and exit code."""
        return machine.run(workspace_id,command,timeout_seconds)

    @mcp.tool()
    def start_process(workspace_id: str, command: str) -> dict:
        """Start a background process (such as a dev server) inside E2B and return its PID."""
        return machine.start_process(workspace_id,command)

    @mcp.tool()
    def list_processes(workspace_id: str) -> dict:
        """List currently running E2B processes."""
        return machine.list_processes(workspace_id)

    @mcp.tool()
    def stop_process(workspace_id: str, pid: int) -> dict:
        """Terminate a background process by PID inside E2B."""
        return machine.stop_process(workspace_id,pid)

    @mcp.tool()
    def preview_url(workspace_id: str, port: int=3000) -> dict:
        """Get HTTPS preview URL for a server listening on 0.0.0.0 in the E2B workspace."""
        return machine.preview_url(workspace_id,port)

    async def health(request: Request):
        return JSONResponse({'ready':True,'service':'dev-mcp','machine':'e2b','transport':'streamable-http'})

    mcp_app=mcp.http_app()
    # OAuth endpoints must be public; all MCP HTTP requests require access token.
    return Starlette(routes=[Route('/health',health),*oauth.routes(),
        Mount('/',app=BearerGate(mcp_app,oauth))],lifespan=mcp_app.lifespan)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(build_app(),host='0.0.0.0',port=int(os.getenv('PORT','8000')),workers=1)
