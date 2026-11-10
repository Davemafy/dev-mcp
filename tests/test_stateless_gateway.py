"""Exercise real FastMCP stateless mounting while mocking external Redis."""
import asyncio
import httpx

def test_stateless_http_app_and_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("STATELESS_GATEWAY", "1")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://devmcp.example")
    monkeypatch.setenv("OWNER_PASSWORD", "p" * 24)
    monkeypatch.setenv("TOKEN_SIGNING_SECRET", "s" * 42)
    monkeypatch.setenv("AUTH_REDIS_URL", "rediss://localhost:6380/0")
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("WORKSPACE_BACKEND", raising=False)
    from shared_state import SharedState
    def offline_state(self, url):
        self.db = None
    monkeypatch.setattr(SharedState, "__init__", offline_state)
    from main import build_app
    app = build_app()
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://devmcp.example"
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["service"] == "dev-mcp"
            metadata = await client.get("/.well-known/oauth-authorization-server")
            assert metadata.status_code == 200
            assert metadata.json()["issuer"] == "https://devmcp.example"
            unauthorized = await client.post("/mcp", json={"jsonrpc":"2.0","id":1,"method":"initialize"})
            assert unauthorized.status_code == 401
    asyncio.run(check())
