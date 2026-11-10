"""Selective, lazy registration of high-value development capabilities.

The base MCP gateway stays small and deployable on any ASGI host. Expensive
browser/desktop processes always run in a signed E2B workspace, never on
the gateway. Stateful features are explicitly disabled on Vercel.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager


def install_capabilities(mcp, machine, oauth):
    """Return (HTTP routes, background jobs, feature status)."""
    provider = os.getenv("WORKSPACE_BACKEND", "e2b").strip().lower()
    serverless = os.getenv("STATELESS_GATEWAY") == "1"
    e2b = provider == "e2b"
    live_requested = os.getenv("LIVE_BROWSER_ENABLED", "1") == "1"
    live = e2b and not serverless and live_requested
    desktop_requested = os.getenv("DESKTOP_TOOLS_ENABLED") == "1"
    jobs_requested = os.getenv("AGENT_JOBS_ENABLED") == "1"

    if desktop_requested and not live:
        raise RuntimeError("Desktop requires a stateful E2B gateway and live browser")
    if jobs_requested and not live:
        raise RuntimeError("Autonomous jobs require a stateful E2B gateway")
    if jobs_requested and not os.path.ismount("/data"):
        raise RuntimeError("Autonomous jobs require a mounted /data volume")

    routes = []
    jobs = None
    features = {
        "browser_journey": e2b,
        "browser_stream": live,
        "desktop": desktop_requested,
        "agent_jobs": jobs_requested,
    }

    if not e2b:
        return routes, jobs, features

    # One round trip for a full browser journey; works on stateless and
    # stateful gateways. Runner is loaded only when actually invoked.
    @mcp.tool()
    def browser_journey(workspace_id: str, url: str,
                        steps: list[dict] | None = None,
                        viewport: str = "mobile") -> dict:
        """Run up to 15 Playwright interactions inside one E2B browser and report failures, mobile/desktop rendering and screenshot location."""
        from browser_journey import browser_journey as run
        return run(machine, workspace_id, url, steps or [], viewport)

    @mcp.tool()
    def browser_capture(workspace_id: str):
        """Return the last verified browser-journey JPEG as an actual MCP image; no arbitrary filesystem read."""
        from fastmcp.utilities.types import Image
        return Image(data=machine.read_capture(workspace_id), format="jpeg")

    if not live:
        return routes, jobs, features

    from stream_browser import BrowserStream
    from adaptive_browser import AdaptiveBrowser
    from browser_proxy import proxy_browser
    from starlette.routing import Route

    streaming = BrowserStream(machine)
    adaptive = AdaptiveBrowser(streaming)

    @mcp.tool()
    def browser_stream_start(workspace_id: str, url: str = "https://example.com",
                             viewport: str = "desktop") -> dict:
        """Create or reuse a persistent Chromium page inside E2B, avoiding a new browser launch per action."""
        return streaming.start(workspace_id, url, viewport)

    @mcp.tool()
    def browser_stream_action(workspace_id: str, action: str,
                              selector: str = "", value: str = "",
                              x: float | None = None, y: float | None = None,
                              dy: float | None = None) -> dict:
        """Interact with the existing Chromium page using bounded, validated actions."""
        return streaming.action(workspace_id, action, selector, value, x, y, dy)

    @mcp.tool()
    def browser_stream_status(workspace_id: str) -> dict:
        """Read the active sandbox browser state without starting a new session."""
        return streaming.status(workspace_id)

    @mcp.tool()
    def browser_stream_stop(workspace_id: str) -> dict:
        """Stop the active browser and revoke its private preview capability."""
        return streaming.stop(workspace_id)

    @mcp.tool()
    def browser_observe(workspace_id: str, base_id: str = "") -> dict:
        """Return live DOM evidence, or changes since the matching observation for lower token usage."""
        return adaptive.observe_delta(workspace_id, base_id or None)

    @mcp.tool()
    def browser_adaptive_run(workspace_id: str, steps: list[dict],
                             max_repairs: int = 2, repair_id: str = "") -> dict:
        """Run a bounded browser program, returning fresh DOM evidence on failure instead of blind retries."""
        return adaptive.execute(workspace_id, steps, max_repairs, repair_id or None)

    async def browser_view(request):
        return await proxy_browser(request, machine, oauth)

    routes += [
        Route("/browser/{session}/", browser_view, methods=["GET", "POST"]),
        Route("/browser/{session}/{path:path}", browser_view, methods=["GET", "POST"]),
    ]

    if desktop_requested:
        from starlette.routing import WebSocketRoute
        from desktop_manager import DesktopManager
        from desktop_gateway import desktop_http, desktop_socket

        desktops = DesktopManager(machine)

        @mcp.tool()
        def desktop_setup(workspace_id: str) -> dict:
            """Install X11/noVNC only inside a signed E2B sandbox."""
            return desktops.setup(workspace_id)

        @mcp.tool()
        def desktop_start(workspace_id: str) -> dict:
            """Start the isolated desktop and obtain an expiring owner-only viewer link."""
            return desktops.start(workspace_id)

        @mcp.tool()
        def desktop_status(workspace_id: str) -> dict:
            """Inspect the desktop state without launching new processes."""
            return desktops.status(workspace_id)

        @mcp.tool()
        def desktop_stop(workspace_id: str) -> dict:
            """Stop the isolated desktop's X11 and VNC processes."""
            return desktops.stop(workspace_id)

        @mcp.tool()
        def desktop_browser_open(workspace_id: str, url: str = "https://example.com") -> dict:
            """Use the persistent Chromium page inside the currently visible desktop."""
            if not desktops.status(workspace_id).get("ready"):
                return {"ready": False, "error": "Start the virtual desktop first."}
            return streaming.start(workspace_id, url, viewport="desktop", headful=True)

        async def desktop_view(request):
            return await desktop_http(request, machine, oauth)

        async def desktop_websocket(websocket):
            return await desktop_socket(websocket, machine, oauth)

        routes += [
            Route("/desktop/{ticket}/", desktop_view, methods=["GET"]),
            Route("/desktop/{ticket}/{path:path}", desktop_view, methods=["GET"]),
            WebSocketRoute("/desktop/{ticket}/websockify", desktop_websocket),
        ]

    if jobs_requested:
        from agent_jobs import AgentJobs
        jobs = AgentJobs(machine, adaptive, enabled=True)

        @mcp.tool()
        def agent_job_submit(workspace_id: str, goal: str,
                             max_steps: int = 8, allow_shell: bool = False,
                             allow_interaction: bool = False) -> dict:
            """Create a bounded, opt-in job with separate browser/shell permissions."""
            return jobs.submit(workspace_id, goal, max_steps, allow_shell, allow_interaction)

        @mcp.tool()
        def agent_job_status(job_id: str) -> dict:
            """Read durable job state and independently observed outcome."""
            return jobs.store.public(job_id) or {"error": "Job not found"}

        @mcp.tool()
        def agent_job_events(job_id: str, limit: int = 30) -> dict:
            """Read the job's durable action, observation and recovery events."""
            return {"job_id": job_id, "events": jobs.store.history(job_id, limit)}

        @mcp.tool()
        def agent_job_list(limit: int = 15) -> dict:
            """List existing autonomous jobs."""
            return {"jobs": jobs.store.list(limit)}

        @mcp.tool()
        def agent_job_control(job_id: str, action: str) -> dict:
            """Pause, resume or cancel an authorized job."""
            return jobs.store.control(job_id, action)

        @mcp.tool()
        def agent_job_runtime() -> dict:
            """Inspect job worker, inference and persistent-storage readiness."""
            return jobs.status()

    return routes, jobs, features


def gateway_lifespan(mcp_app, jobs):
    if jobs is None:
        return mcp_app.lifespan

    @asynccontextmanager
    async def lifespan(app):
        jobs.start()
        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            jobs.worker.stop()

    return lifespan
