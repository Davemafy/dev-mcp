"""dev-mcp: Python FastMCP gateway on Railway, persistent E2B development workspace."""
from __future__ import annotations

import os
from auth import Settings, OAuth, BearerGate
from machine import Machine
from browser import browser_setup as setup_browser, browser_visit as visit_browser
from browser_journey import browser_journey as run_browser_journey
from railway_diagnostics import RailwayInspector
from stream_browser import BrowserStream
from desktop_manager import DesktopManager
from desktop_gateway import desktop_http, desktop_socket
from adaptive_browser import AdaptiveBrowser
from desktop_app import RESOURCE_URI as DESKTOP_UI_URI, desktop_app_html
from agent_jobs import AgentJobs
from browser_proxy import proxy_browser


def build_app(machine_factory=None):
    from fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route, Mount, WebSocketRoute

    settings=Settings.from_env()
    oauth=OAuth(settings)
    machine=Machine(oauth,sandbox_factory=machine_factory)
    railway=RailwayInspector()
    streaming=BrowserStream(machine)
    desktops=DesktopManager(machine)
    adaptive=AdaptiveBrowser(streaming)
    jobs=AgentJobs(machine,adaptive)
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
    def browser_observe(workspace_id: str) -> dict:
        """Inspect current interactive DOM, names and accessibility labels from the live browser to make a grounded plan."""
        return adaptive.observe(workspace_id)

    @mcp.tool()
    def browser_adaptive_run(workspace_id: str, steps: list[dict],
                             max_repairs: int = 2) -> dict:
        """Execute a model-written browser action program on the SAME page. On failure return new DOM evidence so the model can rewrite only failed steps; no arbitrary JS injection."""
        return adaptive.execute(workspace_id, steps, max_repairs)

    # MCP Apps UI is optional; the base tool surface remains usable without it.
    if hasattr(mcp, "resource"):
        from fastmcp.apps import AppConfig, ResourceCSP
        @mcp.tool(app=AppConfig(resourceUri=DESKTOP_UI_URI))
        def desktop_viewer(workspace_id: str) -> dict:
            """Render an optional interactive MCP App for the signed remote desktop. Start the desktop first; other tools work without this UI."""
            state = desktops.status(workspace_id)
            if not state.get("ready"):
                return {"ready": False, "message": "Start the desktop first."}
            return {"ready": True, "viewer_url": desktops._viewer(workspace_id),
                    "display": state.get("display", ":99")}

        @mcp.resource(
            DESKTOP_UI_URI,
            mime_type="text/html;profile=mcp-app",
            app=AppConfig(
                csp=ResourceCSP(
                    frameDomains=[settings.base_url.rstrip("/")],
                    connectDomains=[settings.base_url.rstrip("/")],
                ),
                prefersBorder=False,
            ),
        )
        def desktop_viewer_ui() -> str:
            """Optional ChatGPT-compatible MCP Apps viewer."""
            return desktop_app_html(settings.base_url)

    @mcp.tool()
    def desktop_setup(workspace_id: str) -> dict:
        """Install the X11/noVNC virtual desktop packages in a signed E2B sandbox."""
        return desktops.setup(workspace_id)

    @mcp.tool()
    def desktop_start(workspace_id: str) -> dict:
        """Start a real X11 desktop and return a short-lived private noVNC viewing link."""
        return desktops.start(workspace_id)

    @mcp.tool()
    def desktop_status(workspace_id: str) -> dict:
        """Check the real X11 desktop and VNC service without creating anything."""
        return desktops.status(workspace_id)

    @mcp.tool()
    def desktop_stop(workspace_id: str) -> dict:
        """Stop the virtual desktop supervisor and its VNC and window-manager children."""
        return desktops.stop(workspace_id)

    @mcp.tool()
    def desktop_browser_open(workspace_id: str, url: str = "https://example.com") -> dict:
        """Open Chromium in the SAME virtual desktop seen by noVNC. Run desktop_start and browser_setup first."""
        if not desktops.status(workspace_id).get("ready"):
            return {"ready": False, "error": "Start the virtual desktop first."}
        return streaming.start(workspace_id, url, viewport="desktop", headful=True)

    @mcp.tool()
    def agent_job_submit(workspace_id: str, goal: str,
                         max_steps: int = 8, allow_shell: bool = False,
                         allow_interaction: bool = False) -> dict:
        """Create a bounded durable agent job with explicit shell/edit authorization."""
        return jobs.submit(workspace_id, goal, max_steps, allow_shell, allow_interaction)

    @mcp.tool()
    def agent_job_status(job_id: str) -> dict:
        """Inspect a job's state and its last evidence."""
        return jobs.store.public(job_id) or {"error": "Job not found"}

    @mcp.tool()
    def agent_job_events(job_id: str, limit: int = 30) -> dict:
        """Inspect the recorded execution, failure and restart receipts."""
        return {"job_id": job_id, "events": jobs.store.history(job_id, limit)}

    @mcp.tool()
    def agent_job_list(limit: int = 15) -> dict:
        """List the owner's current and previous jobs."""
        return {"jobs": jobs.store.list(limit)}

    @mcp.tool()
    def agent_job_control(job_id: str, action: str) -> dict:
        """Pause, cancel or explicitly resume a saved job."""
        return jobs.store.control(job_id, action)

    @mcp.tool()
    def agent_job_runtime() -> dict:
        """Read the worker and inference configuration status."""
        return jobs.status()

    @mcp.tool()
    def railway_deployments(limit: int = 10) -> dict:
        """Read recent deployments for the one Railway service configured on the gateway. No mutations."""
        return railway.deployments(limit)

    @mcp.tool()
    def railway_logs(deployment_id: str, kind: str = "build",
                     limit: int = 100) -> dict:
        """Read sanitized build/runtime logs for a verified deployment of that configured Railway service."""
        return railway.logs(deployment_id, kind, limit)

    async def browser_view(request: Request):
        return await proxy_browser(request, machine, oauth)

    async def desktop_view(request: Request):
        return await desktop_http(request, machine, oauth)

    async def desktop_websocket(websocket):
        return await desktop_socket(websocket, machine, oauth)

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
            'agent_jobs_enabled': jobs.enabled and jobs.planner.configured,
        })

    mcp_app=mcp.http_app()
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def runtime_lifespan(app):
        jobs.start()
        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            jobs.worker.stop()
    # OAuth endpoints must be public; all MCP HTTP requests require access token.
    return Starlette(routes=[Route('/health',health),*oauth.routes(),
        Route('/browser/{session}/',browser_view,methods=['GET','POST']),
        Route('/browser/{session}/{path:path}',browser_view,methods=['GET','POST']),
        Route('/desktop/{ticket}/',desktop_view,methods=['GET']),
        Route('/desktop/{ticket}/{path:path}',desktop_view,methods=['GET']),
        WebSocketRoute('/desktop/{ticket}/websockify',desktop_websocket),
        Mount('/',app=BearerGate(mcp_app,oauth))],lifespan=runtime_lifespan)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(build_app(),host='0.0.0.0',port=int(os.getenv('PORT','8000')),workers=1)
