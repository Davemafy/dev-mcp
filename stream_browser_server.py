"""Single-owner cloud browser: persistent Playwright page with authenticated live JPEG streaming.

Runs ONLY inside an E2B workspace. Its HTTPS preview is protected by a
high-entropy session capability exchanged for an HttpOnly/SameSite cookie.
The frame channel and control endpoints share the SAME Chromium page.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import urllib.request
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PORT = 3487
CAPTURE = Path("/home/user/browser-captures/latest.jpg")
ACTIONS = {"navigate", "back", "forward", "reload", "click", "fill",
           "press", "keypress", "type", "scroll", "click_at", "wait_for",
           "assert_text", "inspect_dom"}
MAX_BODY = 3000


def safe_url(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2000:
        raise ValueError("URL must be between 1 and 2000 characters.")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("URL must be http(s) without embedded credentials.")
    return value


def validate_action(payload: dict, viewport: tuple[int, int]) -> dict:
    if not isinstance(payload, dict) or payload.get("action") not in ACTIONS:
        raise ValueError("Unsupported browser action.")
    action = payload["action"]
    result = {"action": action}
    if action == "navigate":
        result["value"] = safe_url(payload.get("value", ""))
    elif action in ("click", "fill", "press", "wait_for", "assert_text"):
        selector = payload.get("selector")
        if not isinstance(selector, str) or not 1 <= len(selector) <= 300:
            raise ValueError("A CSS selector of at most 300 characters is required.")
        result["selector"] = selector
        if action in ("fill", "press", "assert_text"):
            value = payload.get("value")
            if not isinstance(value, str) or not 1 <= len(value) <= 1000:
                raise ValueError("Text must have 1–1000 characters.")
            result["value"] = value
    elif action in ("type", "keypress"):
        value = payload.get("value")
        if not isinstance(value, str) or not 1 <= len(value) <= (80 if action == "keypress" else 1000):
            raise ValueError("Keyboard input size is invalid.")
        result["value"] = value
    elif action == "click_at":
        x, y = payload.get("x"), payload.get("y")
        if (not isinstance(x, (int, float)) or isinstance(x, bool) or
            not isinstance(y, (int, float)) or isinstance(y, bool) or
            x < 0 or y < 0 or x >= viewport[0] or y >= viewport[1]):
            raise ValueError("Click coordinates must fall inside the viewport.")
        result["x"], result["y"] = round(x), round(y)
    elif action == "scroll":
        amount = payload.get("dy")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or abs(amount) > 1600:
            raise ValueError("Scroll delta must be within 1600 pixels.")
        result["dy"] = round(amount)
    return result


@dataclass
class BrowserState:
    token: str
    initial_url: str
    viewport_mode: str = "desktop"
    viewport: tuple[int, int] = field(init=False)
    cookie_value: str = field(init=False)
    ready: bool = False
    failure: str = ""
    title: str = ""
    url: str = ""
    frame: bytes = b""
    frame_version: int = 0
    viewers: int = 0
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    instructions: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=16), repr=False)

    def __post_init__(self):
        self.viewport = (390, 844) if self.viewport_mode == "mobile" else (1440, 900)
        self.cookie_value = hashlib.sha256(("browser:" + self.token).encode()).hexdigest()


def browser_worker(state: BrowserState):
    try:
        from playwright.sync_api import sync_playwright
        CAPTURE.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            headful = os.getenv("DEVMCP_HEADFUL") == "1"
            chrome_process = None
            if headful:
                # A single visible Chromium runs on the X11 desktop. External
                # agent code in this E2B workspace can attach via loopback CDP
                # to this SAME page, without starting another browser.
                executable = p.chromium.executable_path
                chrome_process = subprocess.Popen(
                    [executable, "--no-sandbox", "--disable-dev-shm-usage",
                     "--no-first-run", "--disable-sync",
                     "--remote-debugging-address=127.0.0.1",
                     "--remote-debugging-port=9222",
                     "--user-data-dir=/home/user/.devmcp-chrome-profile",
                     "--window-size=1260,820",
                     state.initial_url],
                    env=dict(os.environ, DISPLAY=":99"),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(45):
                    if chrome_process.poll() is not None:
                        break
                    try:
                        with urllib.request.urlopen(
                            "http://127.0.0.1:9222/json/version", timeout=1
                        ) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(.3)
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
            else:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = browser.new_context(
                    viewport={"width": state.viewport[0], "height": state.viewport[1]},
                    device_scale_factor=1,
                    has_touch=state.viewport_mode == "mobile",
                    is_mobile=state.viewport_mode == "mobile",
                )
                page = context.new_page()
            page.on("pageerror", lambda e: state.errors.append(type(e).__name__))
            try:
                page.goto(state.initial_url, wait_until="domcontentloaded", timeout=22000)
                with state.lock:
                    state.ready = True
                last_capture = 0.0
                while True:
                    try:
                        item = state.instructions.get(timeout=0.55)
                    except queue.Empty:
                        item = None
                    if item is not None:
                        payload, result, done = item
                        try:
                            action = payload["action"]
                            selector = payload.get("selector", "")
                            value = payload.get("value", "")
                            locator = page.locator(selector).first if selector else None
                            if action == "navigate":
                                page.goto(value, wait_until="domcontentloaded", timeout=18000)
                            elif action == "back":
                                page.go_back(wait_until="domcontentloaded", timeout=18000)
                            elif action == "forward":
                                page.go_forward(wait_until="domcontentloaded", timeout=18000)
                            elif action == "reload":
                                page.reload(wait_until="domcontentloaded", timeout=18000)
                            elif action == "click":
                                locator.click(timeout=8000)
                            elif action == "fill":
                                locator.fill(value, timeout=8000)
                            elif action == "press":
                                locator.press(value, timeout=8000)
                            elif action == "keypress":
                                page.keyboard.press(value)
                            elif action == "wait_for":
                                locator.wait_for(state="visible", timeout=8000)
                            elif action == "assert_text":
                                if value.casefold() not in locator.inner_text(timeout=8000).casefold():
                                    raise AssertionError("Expected content not present.")
                            elif action == "type":
                                page.keyboard.insert_text(value)
                            elif action == "scroll":
                                page.mouse.wheel(0, payload["dy"])
                            elif action == "click_at":
                                page.mouse.click(payload["x"], payload["y"])
                            elif action == "inspect_dom":
                                result["elements"] = page.locator(
                                    "a, button, input, textarea, select, [role=button]"
                                ).evaluate_all("""els => els.slice(0, 60).map(el => ({
                                    tag: el.tagName.toLowerCase(),
                                    role: el.getAttribute('role') || '',
                                    name: (el.getAttribute('aria-label') ||
                                      el.innerText || el.value || '').slice(0, 110),
                                    id: (el.id || '').slice(0, 100),
                                    testId: (el.getAttribute('data-testid') || '').slice(0, 100)
                                }))""")
                                result["visible_text"] = page.locator("body").inner_text(timeout=3500)[:1500]
                            result.update({"ok": True, "action": action, "url": page.url,
                                           "title": page.title()[:200]})
                        except Exception as e:
                            result.update({"ok": False, "action": payload["action"],
                                           "error": type(e).__name__})
                    # Idle headless Chromium should not continuously re-render a
                    # static page on the smallest E2B machine. Active viewers
                    # get roughly one new frame per second; actions capture now.
                    with state.lock:
                        active_viewers = state.viewers
                    now = time.monotonic()
                    interval = 1.0 if active_viewers else 4.0
                    if item is None and now - last_capture < interval:
                        continue
                    last_capture = now
                    try:
                        if headful:
                            # Screenshot the actual X11 framebuffer, so the agent
                            # inspects the same desktop as the human VNC viewer.
                            capture = subprocess.run(
                                ["scrot", "-q", "50", str(CAPTURE)],
                                env=dict(os.environ, DISPLAY=":99"),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=4, check=True,
                            )
                            jpeg = CAPTURE.read_bytes()
                        else:
                            jpeg = page.screenshot(type="jpeg", quality=55,
                                                   full_page=False, timeout=4500)
                        with state.lock:
                            state.frame = jpeg
                            state.frame_version += 1
                            state.url = page.url[:2000]
                            state.title = page.title()[:200]
                        if not headful:
                            tmp = CAPTURE.with_suffix(".new")
                            tmp.write_bytes(jpeg)
                            os.replace(tmp, CAPTURE)
                    except Exception as error:
                        with state.lock:
                            state.errors.append("Capture:" + type(error).__name__)
                            state.errors = state.errors[-8:]
                    finally:
                        if item is not None:
                            # Only acknowledge an action after its new visual
                            # frame has been captured (or capture has failed).
                            result["frame_version"] = state.frame_version
                            done.set()
            finally:
                if not headful:
                    context.close()
                browser.close()
                if chrome_process is not None:
                    chrome_process.terminate()
                    try:
                        chrome_process.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        chrome_process.kill()
    except Exception as error:
        with state.lock:
            state.failure = type(error).__name__
            state.ready = False


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Dev MCP browser</title>
<link href="style.css" rel="stylesheet"></head>
<body><div id="login" hidden>Private browser session. Open the link from Dev MCP to connect.</div>
<main id="app" hidden><header class="toolbar"><strong>Cloud browser</strong><div class="actions">
<button data-action="back" aria-label="Go back">←</button><button data-action="forward" aria-label="Go forward">→</button>
<button data-action="reload" aria-label="Reload">↻</button></div>
<form id="address-form"><label class="sr" for="address">Address</label><input id="address" autocomplete="off" spellcheck="false" aria-label="Browser address"><button>Go</button></form>
<span id="status">Connecting</span></header>
<div class="screenwrap"><img id="screen" src="stream" alt="Live browser view" draggable="false"></div>
<footer><form id="type-form"><label class="sr" for="typing">Type in focused field</label>
<input id="typing" placeholder="Type into the selected field" autocomplete="off"><button>Type</button></form>
<button id="enter" type="button">Enter</button><span id="page-title"></span></footer></main>
<script src="app.js" defer></script></body></html>"""

STYLE = """*{box-sizing:border-box}body{margin:0;background:#eceeea;color:#242925;font-family:system-ui,sans-serif}
#login{padding:26px;text-align:center}main{min-height:100vh;display:flex;flex-direction:column}#app[hidden],#login[hidden]{display:none!important}
.toolbar{display:flex;align-items:center;gap:12px;padding:12px 18px;background:#fafbf8;flex-wrap:wrap}
.toolbar strong{font-size:15px;letter-spacing:-.02em}.actions{display:flex;gap:5px}
button{cursor:pointer;font:inherit;border:0;color:#242925;background:#e4e9e3;border-radius:10px;padding:9px 12px}
button:hover{background:#d8e0d7}button:focus-visible,input:focus-visible{outline:2px solid #446a55;outline-offset:2px}
#address-form{display:flex;flex:1;min-width:180px;gap:6px}
input{font:inherit;border:1px solid #dce2dc;border-radius:10px;background:white;padding:9px 11px;min-width:0;width:100%}
#status{color:#69736b;font-size:12px;max-width:220px}.screenwrap{flex:1;display:flex;align-items:center;justify-content:center;padding:18px;overflow:auto}
#screen{display:block;width:auto;max-width:100%;max-height:calc(100vh - 170px);height:auto;box-shadow:0 14px 50px #18221a24;
border-radius:6px;cursor:crosshair;touch-action:manipulation;background:#fff}
footer{display:flex;align-items:center;gap:10px;background:#fafbf8;padding:12px 18px}
#type-form{display:flex;flex:1;gap:8px;max-width:600px}#page-title{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;max-width:30ch;font-size:12px;color:#707b72}
.sr{position:absolute;clip:rect(0,0,0,0);width:1px;height:1px;overflow:hidden}
@media(max-width:640px){.toolbar{padding:11px}.screenwrap{padding:10px}
#screen{max-height:calc(100vh - 220px)}footer{padding:10px}#page-title{display:none}}
"""

JS = """const app=document.querySelector('#app'),login=document.querySelector('#login');
const status=document.querySelector('#status'),img=document.querySelector('#screen');
let width=1440,height=900;
async function send(action,extra={}) {
 const r=await fetch('control',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({action,...extra})});
 const data=await r.json(); status.textContent=data.ok?('Done · '+action):('Unavailable · '+(data.error||action));
 return data;
}
async function sync(){
 try{const r=await fetch('state',{cache:'no-store',credentials:'same-origin'});
 if(r.status===401){app.hidden=true;login.hidden=false;return}
 const d=await r.json(); app.hidden=false;login.hidden=true;
 width=d.width||width;height=d.height||height;
 document.querySelector('#address').value=d.url||'';
 document.querySelector('#page-title').textContent=d.title||'';
 if(!d.ready) status.textContent=d.failure?('Unavailable · '+d.failure):'Opening browser';
 else if(status.textContent==='Connecting')status.textContent='Connected';
 }catch(e){status.textContent='Connection interrupted'}
}
async function init(){
 const token=new URLSearchParams(location.hash.slice(1)).get('key');
 if(token){history.replaceState(null,'',location.pathname);await fetch('session',{
 method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:token})
 }).catch(()=>{});}
 await sync();setInterval(sync,2500);
}
document.querySelectorAll('[data-action]').forEach(b=>b.addEventListener('click',()=>send(b.dataset.action)));
document.querySelector('#address-form').addEventListener('submit',async e=>{e.preventDefault();const value=document.querySelector('#address').value;
if(!value)return;await send('navigate',{value});await sync()});
document.querySelector('#type-form').addEventListener('submit',async e=>{e.preventDefault();const el=document.querySelector('#typing');if(!el.value)return;
await send('type',{value:el.value});el.value=''});
document.querySelector('#enter').addEventListener('click',()=>send('keypress',{value:'Enter'}));
img.addEventListener('click',async e=>{const rect=img.getBoundingClientRect();
const x=Math.max(0,Math.min(width-1,Math.round((e.clientX-rect.left)*width/rect.width)));
const y=Math.max(0,Math.min(height-1,Math.round((e.clientY-rect.top)*height/rect.height)));
await send('click_at',{x,y})});
let wheelTimer=0;
img.addEventListener('wheel',e=>{e.preventDefault();if(wheelTimer)return;wheelTimer=setTimeout(()=>wheelTimer=0,120);
send('scroll',{dy:Math.max(-1200,Math.min(1200,Math.round(e.deltaY)))})},{passive:false});
init();
"""


def build_handler(state: BrowserState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PrivateBrowser/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            # Avoid accidentally logging keys, cookies, navigation URLs or typed text.
            pass

        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            super().end_headers()

        def authorized(self):
            bearer = self.headers.get("Authorization", "")
            if bearer.startswith("Bearer "):
                return hmac.compare_digest(bearer[7:], state.token)
            raw = self.headers.get("Cookie", "")
            try:
                jar = cookies.SimpleCookie(raw)
                supplied = jar.get("devmcp_browser")
                return supplied is not None and hmac.compare_digest(supplied.value, state.cookie_value)
            except Exception:
                return False

        def same_origin(self):
            origin = self.headers.get("Origin")
            if not origin:
                return False
            parsed = urlsplit(origin)
            host = self.headers.get("Host", "").lower()
            return parsed.scheme == "https" and parsed.netloc.lower() == host

        def response(self, code, data, mimetype="application/json", headers=None):
            value = (json.dumps(data).encode() if mimetype == "application/json"
                     else data if isinstance(data, bytes) else data.encode())
            self.send_response(code)
            self.send_header("Content-Type", mimetype)
            self.send_header("Content-Length", str(len(value)))
            for key, val in (headers or {}).items():
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(value)

        def reject(self, code=401):
            self.response(code, {"error": "Unauthorized" if code == 401 else "Forbidden"})

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/app.js":
                return self.response(200, JS, "text/javascript; charset=utf-8")
            if path == "/style.css":
                return self.response(200, STYLE, "text/css; charset=utf-8")
            if path == "/":
                return self.response(200, HTML, "text/html; charset=utf-8", {
                    "Content-Security-Policy":
                        "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; "
                        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                })
            if not self.authorized():
                return self.reject()
            if path in ("/state", "/health"):
                with state.lock:
                    data = {"ready": state.ready, "failure": state.failure,
                            "url": state.url, "title": state.title,
                            "frame_version": state.frame_version,
                            "width": state.viewport[0], "height": state.viewport[1],
                            "errors": state.errors[-5:]}
                return self.response(200 if state.ready else 503, data)
            if path == "/snapshot":
                with state.lock:
                    frame = state.frame
                return self.response(200, frame, "image/jpeg") if frame else self.reject(503)
            if path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                previous = -1
                with state.lock:
                    state.viewers += 1
                try:
                    for _ in range(800):
                        with state.lock:
                            frame, version = state.frame, state.frame_version
                        if frame and version != previous:
                            part = (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                                    str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                            self.wfile.write(part)
                            self.wfile.flush()
                            previous = version
                        time.sleep(0.55)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with state.lock:
                        state.viewers = max(0, state.viewers - 1)
                return
            self.reject(404)

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.response(400, {"error": "Invalid body length"})
            if size < 1 or size > MAX_BODY:
                return self.response(413, {"error": "Body too large"})
            if path != "/session" and not self.authorized():
                return self.reject()
            if path == "/session" and not self.same_origin():
                return self.reject(403)
            if path == "/control" and not self.headers.get("Authorization", "").startswith("Bearer "):
                if not self.same_origin():
                    return self.reject(403)
            try:
                payload = json.loads(self.rfile.read(size))
            except (TypeError, ValueError):
                return self.response(400, {"error": "Invalid JSON"})
            if path == "/session":
                key = payload.get("key") if isinstance(payload, dict) else None
                if not isinstance(key, str) or not hmac.compare_digest(key, state.token):
                    return self.reject()
                cookie = ("devmcp_browser=" + state.cookie_value +
                          "; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=1800")
                return self.response(200, {"ok": True}, headers={"Set-Cookie": cookie})
            if path != "/control":
                return self.reject(404)
            try:
                action = validate_action(payload, state.viewport)
            except (ValueError, TypeError):
                return self.response(400, {"error": "Invalid action"})
            done = threading.Event()
            result = {}
            try:
                state.instructions.put_nowait((action, result, done))
            except queue.Full:
                return self.response(429, {"error": "Browser is busy"})
            if not done.wait(22):
                return self.response(504, {"error": "Browser action timed out"})
            return self.response(200 if result.get("ok") else 422, result)
    return Handler


def serve(config: dict):
    token = config.get("token")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("Browser session token not configured.")
    url = safe_url(config.get("url", ""))
    viewport = config.get("viewport", "desktop")
    if viewport not in ("desktop", "mobile"):
        raise ValueError("Invalid viewport.")
    state = BrowserState(token, url, viewport)
    thread = threading.Thread(target=browser_worker, args=(state,), daemon=True)
    thread.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), build_handler(state))
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    config = json.loads(Path(ap.parse_args().config).read_text())
    serve(config)
