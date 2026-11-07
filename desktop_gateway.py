"""Signed HTTPS/WebSocket gateway for the full E2B desktop.

The browser viewer never receives E2B's inbound traffic access token.
Gateway route validates a short-lived desktop-view ticket and fixes the
sandbox port to noVNC; WebSockets are forwarded as bounded binary frames.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from auth import OAuthError
from browser_proxy import allowed_origin
from desktop_runtime import PORT

HOST_RE = re.compile(r"^[A-Za-z0-9.-]{8,255}$")
SANDBOX_RE = re.compile(r"^[A-Za-z0-9-]{8,80}$")
ASSETS = ("app/", "core/", "vendor/", "images/", "favicon.ico",
          "vnc.html", "vnc_lite.html", "vnc_auto.html")
MAX_FRAME = 1024 * 1024


def verify_ticket(oauth, ticket):
    claims = oauth.verify(ticket, "desktop-view")
    sandbox_id = claims.get("sandbox_id")
    if not isinstance(sandbox_id, str) or not SANDBOX_RE.fullmatch(sandbox_id):
        raise OAuthError("Invalid desktop scope")
    return sandbox_id


def get_upstream(oauth, machine, ticket):
    sandbox_id = verify_ticket(oauth, ticket)
    sandbox = machine.sandbox_factory.connect(sandbox_id)
    token = getattr(sandbox, "traffic_access_token", None)
    if not isinstance(token, str) or not token:
        raise RuntimeError("Sandbox traffic token unavailable")
    host = sandbox.get_host(PORT)
    if not HOST_RE.fullmatch(host):
        raise RuntimeError("Invalid E2B preview host")
    return host, token


def allowed_asset(path):
    if not isinstance(path, str) or len(path) > 700:
        return False
    if path.startswith("/") or ".." in path.split("/"):
        return False
    return any(path == prefix or path.startswith(prefix) for prefix in ASSETS)


async def desktop_http(request: Request, machine, oauth):
    if request.method != "GET":
        return JSONResponse({"error": "Not allowed"}, status_code=405)
    path = request.path_params.get("path", "") or "vnc_lite.html"
    if not allowed_asset(path) or path == "websockify":
        return JSONResponse({"error": "Asset unavailable"}, status_code=404)
    try:
        host, token = get_upstream(oauth, machine, request.path_params["ticket"])
    except (OAuthError, ValueError):
        return JSONResponse({"error": "Unauthorized desktop viewer"}, status_code=401)
    except Exception:
        return JSONResponse({"error": "Desktop unavailable"}, status_code=503)
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
            response = await client.get(
                "https://" + host + "/" + path,
                headers={"e2b-traffic-access-token": token},
            )
            if len(response.content) > 2_000_000:
                return JSONResponse({"error": "Asset too large"}, status_code=502)
            headers = {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
            }
            return Response(
                content=response.content, status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/octet-stream"),
                headers=headers,
            )
    except (httpx.HTTPError, OSError):
        return JSONResponse({"error": "Desktop transport unavailable"}, status_code=502)


async def desktop_socket(websocket: WebSocket, machine, oauth):
    request_origin = websocket.headers.get("origin")
    if not allowed_origin(request_origin, oauth.cfg.base_url):
        await websocket.close(code=1008)
        return
    try:
        host, token = get_upstream(oauth, machine, websocket.path_params["ticket"])
    except Exception:
        await websocket.close(code=1008)
        return
    requested_protocols = [
        value.strip() for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if value.strip() in ("binary", "base64")
    ]
    protocol = "binary" if "binary" in requested_protocols else None
    try:
        upstream = await connect(
            "wss://" + host + "/websockify",
            additional_headers={"e2b-traffic-access-token": token},
            origin="https://" + host,
            subprotocols=[protocol] if protocol else None,
            open_timeout=12,
            max_size=MAX_FRAME,
            compression=None,
            ping_interval=20,
        )
    except Exception:
        await websocket.close(code=1013)
        return

    await websocket.accept(subprotocol=protocol if upstream.subprotocol == protocol else None)

    async def from_viewer():
        try:
            async for message in websocket.iter_bytes():
                if len(message) > MAX_FRAME:
                    break
                await upstream.send(message)
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def to_viewer():
        try:
            async for message in upstream:
                if len(message) > MAX_FRAME:
                    break
                if isinstance(message, str):
                    await websocket.send_text(message)
                else:
                    await websocket.send_bytes(message)
        except (ConnectionClosed, RuntimeError, WebSocketDisconnect):
            pass

    try:
        tasks = [asyncio.create_task(from_viewer()), asyncio.create_task(to_viewer())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await upstream.close()
        try:
            await websocket.close(code=1000)
        except RuntimeError:
            pass
