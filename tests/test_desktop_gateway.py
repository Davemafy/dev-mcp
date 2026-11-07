"""Authenticated noVNC HTTP/WebSocket relay tests."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

import desktop_gateway as gateway
from auth import OAuthError


class FakeOAuth:
    def __init__(self):
        self.cfg = type("Cfg", (), {"base_url": "https://gateway.example"})()

    def verify(self, token, typ):
        if token != "signed" or typ != "desktop-view":
            raise OAuthError("Invalid")
        return {"sandbox_id": "sandbox-test-123"}


class FakeSandbox:
    traffic_access_token = "e2b-traffic-secret"

    def get_host(self, port):
        assert port == 3488
        return "3488-sandbox-test-123.e2b.app"


class FakeMachine:
    def __init__(self):
        self.sandbox_factory = self
    def connect(self, sandbox_id):
        assert sandbox_id == "sandbox-test-123"
        return FakeSandbox()


def test_asset_allowlist_and_ticket_scope():
    assert gateway.allowed_asset("vnc_lite.html")
    assert gateway.allowed_asset("core/rfb.js")
    assert gateway.allowed_asset("app/styles/base.css")
    assert not gateway.allowed_asset("../etc/passwd")
    assert not gateway.allowed_asset("websockify")
    assert not gateway.allowed_asset("secrets.json")
    assert not gateway.allowed_asset("/core/rfb.js")
    with pytest.raises(OAuthError):
        gateway.verify_ticket(FakeOAuth(), "expired")
    assert gateway.verify_ticket(FakeOAuth(), "signed") == "sandbox-test-123"


def test_http_proxy_injects_e2b_header_and_never_exposes_it(monkeypatch):
    calls = []
    original = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        assert request.headers["e2b-traffic-access-token"] == "e2b-traffic-secret"
        assert request.url.host == "3488-sandbox-test-123.e2b.app"
        return httpx.Response(
            200, text="<html>noVNC</html>",
            headers={"Content-Type": "text/html"},
        )

    def factory(**kwargs):
        kwargs.pop("trust_env", None)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(gateway.httpx, "AsyncClient", factory)

    async def route(request):
        return await gateway.desktop_http(request, FakeMachine(), FakeOAuth())

    app = Starlette(routes=[Route("/desktop/{ticket}/{path:path}", route)])
    with TestClient(app) as client:
        denied = client.get("/desktop/expired/vnc_lite.html")
        assert denied.status_code == 401
        assert not calls
        invalid = client.get("/desktop/signed/secrets.json")
        assert invalid.status_code == 404
        response = client.get("/desktop/signed/vnc_lite.html")
        assert response.status_code == 200
        assert b"noVNC" in response.content
        assert "e2b-traffic-secret" not in response.text
        assert len(calls) == 1


def test_websocket_relay_passes_binary_vnc_data_and_enforces_origin(monkeypatch):
    created = []

    class FakeUpstream:
        subprotocol = "binary"
        def __init__(self):
            self.queue = asyncio.Queue()
            self.closed = False
        async def send(self, packet):
            await self.queue.put(b"RFB:" + packet)
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.closed:
                raise StopAsyncIteration
            return await self.queue.get()
        async def close(self):
            self.closed = True

    async def fake_connect(url, **kwargs):
        assert url == "wss://3488-sandbox-test-123.e2b.app/websockify"
        assert kwargs["additional_headers"]["e2b-traffic-access-token"] == "e2b-traffic-secret"
        assert kwargs["subprotocols"] == ["binary"]
        upstream = FakeUpstream()
        created.append(upstream)
        return upstream

    monkeypatch.setattr(gateway, "connect", fake_connect)

    async def route(ws):
        return await gateway.desktop_socket(ws, FakeMachine(), FakeOAuth())

    app = Starlette(routes=[
        WebSocketRoute("/desktop/{ticket}/websockify", route)
    ])
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/desktop/signed/websockify",
                headers={"Origin": "https://malicious.example"},
                subprotocols=["binary"],
            ):
                pass
        with client.websocket_connect(
            "/desktop/signed/websockify",
            headers={"Origin": "https://gateway.example"},
            subprotocols=["binary"],
        ) as ws:
            ws.send_bytes(b"hello-vnc")
            assert ws.receive_bytes() == b"RFB:hello-vnc"
        assert len(created) == 1
