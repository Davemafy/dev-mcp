"""MCP Apps viewer metadata and safe resource HTML."""
from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from desktop_app import RESOURCE_URI, desktop_app_html


def test_ui_filters_untrusted_links_and_requires_user_gesture():
    content = desktop_app_html("https://gateway.example")
    assert "<iframe" in content
    assert '"https://gateway.example"' in content
    assert 'u.origin !== gateway' in content
    assert '!u.pathname.startsWith("/desktop/")' in content
    assert 'document.getElementById("actions").hidden = false' in content
    assert 'load.addEventListener("click"' in content
    assert 'iframe.src = viewerUrl' in content
    assert "localStorage" not in content
    assert "remote-debugging" not in content
    assert "referrerpolicy=\"no-referrer\"" in content
    assert RESOURCE_URI.startswith("ui://")


def test_real_fastmcp_registers_ui_resource_csp_and_render_tool(monkeypatch, tmp_path):
    import main

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://gateway.example")
    monkeypatch.setenv("OWNER_PASSWORD", "p" * 30)
    monkeypatch.setenv("TOKEN_SIGNING_SECRET", "s" * 42)
    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "owner-auth.sqlite"))
    instances = []
    original = FastMCP.__init__

    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(FastMCP, "__init__", capture)

    class FakeSandbox:
        @staticmethod
        def create(**kwargs):
            raise RuntimeError("Unused")
        @staticmethod
        def connect(sandbox_id):
            raise RuntimeError("Unused")

    main.build_app(machine_factory=FakeSandbox)
    assert instances
    async def check():
        tools = await instances[0].list_tools()
        resources = await instances[0].list_resources()
        renders = [tool for tool in tools if tool.name == "desktop_viewer"]
        assert len(renders) == 1
        assert renders[0].meta["ui"]["resourceUri"] == RESOURCE_URI
        matching = [r for r in resources if str(r.uri) == RESOURCE_URI]
        assert len(matching) == 1
        assert matching[0].mime_type == "text/html;profile=mcp-app"
        domains = matching[0].meta["ui"]["csp"]["frameDomains"]
        assert domains == ["https://gateway.example"]
    asyncio.run(check())
