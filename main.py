"""dev-mcp: Python FastMCP gateway on Railway, persistent E2B development workspace."""
from __future__ import annotations

import os
from auth import Settings, OAuth, BearerGate
from machine import Machine
from browser import browser_setup as setup_browser, browser_visit as visit_browser
from browser_journey import browser_journey as run_browser_journey
from railway_diagnostics import RailwayInspector
from stream_browser import BrowserStream


def build_app(machine_factory=None):
    from fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route, Mount

    settings=Settings.from_env()
    oauth=OAuth(settings)
    machine=Machine(oauth,sandbox_factory=machine_factory)
    railway=RailwayInspector()
    streaming=BrowserStream(machine)
    mcp=FastMCP('dev-mcp · E2B cloud machine')

    @mcp.tool()
    def workspace_create() -> dict:
        """Create a fresh disposable Linux workspace in E2B; pass its workspace_id to all subsequent tools."""
        return machine.create()

    @mcp.tool()
    def workspace_status(workspace_id: str) -> dict:
        """Check an existing workspace; use workspace_id='recover' to discover surviving owner workspaces and obtain fresh signed handles without creating a new machine."""
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

    @mcp.tool()
    def browser_setup(workspace_id: str) -> dict:
        """Install headless Chromium inside this E2B workspace (once per workspace)."""
        return setup_browser(machine, workspace_id)

    @mcp.tool()
    def browser_visit(workspace_id: str, url: str, action: str = "inspect",
                      selector: str = "", value: str = "") -> dict:
        """Open a web page in E2B Chromium; optionally click/fill CSS selector; return title, text, links, and screenshot path. Run browser_setup first."""
        return visit_browser(machine, workspace_id, url, action, selector, value)

    @mcp.tool()
    def browser_journey(workspace_id: str, url: str,
                        steps: list[dict] | None = None,
                        viewport: str = "mobile") -> dict:
        """Run up to 15 sequential Chromium interactions in one sandbox browser session, capturing console failures and a screenshot. Call browser_setup first."""
        return run_browser_journey(machine, workspace_id, url, steps or [], viewport)

    @mcp.tool()
    def browser_capture(workspace_id: str):
        """Return the last browser screenshot as an actual image from this signed E2B workspace."""
        from fastmcp.utilities.types import Image
        return Image(data=machine.read_capture(workspace_id), format="jpeg")

    @mcp.tool()
    def browser_stream_start(workspace_id: str, url: str = "https://example.com",
                             viewport: str = "desktop") -> dict:
        """Start or reconnect a private live browser in a signed E2B workspace."""
        return streaming.start(workspace_id, url, viewport)

    @mcp.tool()
    def browser_stream_action(workspace_id: str, action: str,
                              selector: str = "", value: str = "",
                              x: float | None = None, y: float | None = None,
                              dy: float | None = None) -> dict:
        """Control the active browser page using selectors, keyboard or coordinates."""
        return streaming.action(workspace_id, action, selector, value, x, y, dy)

    @mcp.tool()
    def browser_stream_status(workspace_id: str) -> dict:
        """Read browser state, page title, viewport and non-sensitive errors."""
        return streaming.status(workspace_id)

    @mcp.tool()
    def browser_stream_stop(workspace_id: str) -> dict:
        """Stop the active browser process and revoke its preview session."""
        return streaming.stop(workspace_id)

    @mcp.tool()
    def railway_deployments(limit: int = 10) -> dict:
        """Read recent deployments for the one Railway service configured on the gateway. No mutations."""
        return railway.deployments(limit)

    @mcp.tool()
    def railway_logs(deployment_id: str, kind: str = "build",
                     limit: int = 100) -> dict:
        """Read sanitized build/runtime logs for a verified deployment of that configured Railway service."""
        return railway.logs(deployment_id, kind, limit)

    async def health(request: Request):
        return JSONResponse({
            'ready': True,
            'service': 'dev-mcp',
            'machine': 'e2b',
            'transport': 'streamable-http',
            'e2b_configured': machine.e2b_configured,
            'workspace_available': machine.e2b_configured,
            'workspace_recovery_available': machine.e2b_configured,
            'workspace_recovery_version': 1,
            'railway_diagnostics_configured': railway.configured,
        })

    mcp_app=mcp.http_app()
    # OAuth endpoints must be public; all MCP HTTP requests require access token.
    return Starlette(routes=[Route('/health',health),*oauth.routes(),
        Mount('/',app=BearerGate(mcp_app,oauth))],lifespan=mcp_app.lifespan)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(build_app(),host='0.0.0.0',port=int(os.getenv('PORT','8000')),workers=1)
