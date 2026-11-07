"""Railway gateway authenticated reverse proxy for protected E2B live browsers."""
from __future__ import annotations

import httpx
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.routing import Route

from auth import OAuthError
from browser_proxy import verify_viewer, allowed_origin, proxy_browser


class FakeOAuth:
    def __init__(self):
        self.cfg = type("Settings", (), {"base_url": "https://gateway.example"})()

    def verify(self, ticket, kind):
        if ticket != "good-ticket" or kind != "browser-view":
            raise OAuthError("Invalid")
        return {"sandbox_id": "e2b-sandbox-1234"}


class FakeSandbox:
    traffic_access_token = "private-e2b-token"

    def get_host(self, port):
        assert port == 3487
        return "3487-e2b-sandbox-1234.e2b.app"


class FakeMachine:
    def __init__(self):
        self.sandbox_factory = self

    def connect(self, sandbox_id):
        assert sandbox_id == "e2b-sandbox-1234"
        return FakeSandbox()


def client_for(requests):
    def handle(request):
        requests.append(request)
        assert request.url.host == "3487-e2b-sandbox-1234.e2b.app"
        assert request.headers["e2b-traffic-access-token"] == "private-e2b-token"
        if request.url.path == "/stream":
            return httpx.Response(200, headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame"
            }, stream=httpx.ByteStream(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8JPEG\r\n"))
        if request.url.path == "/session":
            assert request.headers["origin"] == "https://3487-e2b-sandbox-1234.e2b.app"
            return httpx.Response(200, headers={
                "Content-Type": "application/json",
                "Set-Cookie": "devmcp_browser=abc; Secure; HttpOnly; SameSite=Strict",
            }, content=b'{"ok":true}')
        return httpx.Response(200, headers={"Content-Type": "text/html"},
                              content=b"<title>Private browser</title>")
    return lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(handle), **kwargs
    )


def test_view_ticket_must_be_signed_for_this_gateway():
    oauth = FakeOAuth()
    assert verify_viewer(oauth, "good-ticket") == "e2b-sandbox-1234"
    try:
        verify_viewer(oauth, "bad-ticket")
        assert False
    except OAuthError:
        pass
    assert allowed_origin("https://gateway.example", oauth.cfg.base_url)
    assert not allowed_origin("https://attacker.example", oauth.cfg.base_url)
    assert not allowed_origin("http://gateway.example", oauth.cfg.base_url)
    assert not allowed_origin(None, oauth.cfg.base_url)


def make_client(requests):
    async def view(request):
        return await proxy_browser(request, FakeMachine(), FakeOAuth(),
                                   client_factory=client_for(requests))
    app = Starlette(routes=[
        Route("/browser/{session}/", view, methods=["GET", "POST"]),
        Route("/browser/{session}/{path:path}", view, methods=["GET", "POST"]),
    ])
    return TestClient(app)


def test_browser_view_requires_signed_ticket_and_injects_token_privately():
    calls = []
    with make_client(calls) as client:
        denied = client.get("/browser/bad-ticket/")
        assert denied.status_code == 401
        assert not calls
        page = client.get("/browser/good-ticket/")
        assert page.status_code == 200
        assert b"Private browser" in page.content
        assert len(calls) == 1
        assert "private-e2b-token" not in page.text
        assert calls[0].headers["e2b-traffic-access-token"] == "private-e2b-token"
        assert calls[0].url.path == "/"


def test_browser_post_checks_origin_and_rewrites_for_protected_sandbox():
    calls = []
    with make_client(calls) as client:
        unauthorized = client.post("/browser/good-ticket/session",
                                   headers={"Origin": "https://attacker.example"},
                                   json={"key": "example"})
        assert unauthorized.status_code == 403
        assert not calls
        response = client.post("/browser/good-ticket/session",
                               headers={"Origin": "https://gateway.example"},
                               json={"key": "example"})
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["Set-Cookie"]
        assert len(calls) == 1
        assert calls[0].headers["Origin"] == "https://3487-e2b-sandbox-1234.e2b.app"
        assert calls[0].headers["e2b-traffic-access-token"] == "private-e2b-token"


def test_stream_forwards_actual_multipart_jpeg_without_reencoding_or_secrets():
    calls = []
    with make_client(calls) as client:
        result = client.get("/browser/good-ticket/stream")
        assert result.status_code == 200
        assert "multipart/x-mixed-replace" in result.headers["content-type"]
        assert b"\xff\xd8JPEG" in result.content
        assert "private-e2b-token" not in result.text
