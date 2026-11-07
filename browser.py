"""Playwright automation executed strictly inside an authenticated E2B workspace.

No browser, subprocess, or untrusted URL is executed by the Railway gateway.
"""
from __future__ import annotations

import base64
import json
import shlex
from urllib.parse import urlsplit

_BROWSER_RUNNER = r'''
import json
import os
import sys
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

payload = json.loads(sys.argv[1])
url = payload["url"]
action = payload["action"]
selector = payload["selector"]
value = payload["value"]
os.makedirs("/home/user/browser-captures", exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
        if action == "click":
            page.locator(selector).first.click(timeout=8000)
        elif action == "fill":
            page.locator(selector).first.fill(value, timeout=8000)
        page.wait_for_timeout(300)
        path = "/home/user/browser-captures/latest.jpg"
        page.screenshot(path=path, type="jpeg", quality=65, full_page=False, timeout=12000)
        print(json.dumps({
            "ok": True, "url": page.url, "title": page.title(),
            "status": response.status if response else None,
            "text": page.locator("body").inner_text(timeout=5000)[:6000],
            "screenshot_path": path,
            "links": page.locator("a[href]").evaluate_all(
                "(nodes) => nodes.slice(0, 12).map(a => ({text: a.innerText.slice(0, 80), href: a.href}))"
            )
        }))
    finally:
        browser.close()
'''

def validate_browser_input(url: str, action: str, selector: str, value: str):
    if not isinstance(url, str) or len(url) > 2000:
        raise ValueError("URL must be a string of at most 2000 characters")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("URL must be an http(s) URL without embedded credentials")
    if action not in ("inspect", "click", "fill"):
        raise ValueError("Action must be inspect, click, or fill")
    if not isinstance(selector, str) or len(selector) > 300:
        raise ValueError("Invalid selector")
    if action != "inspect" and not selector.strip():
        raise ValueError("Click/fill requires a CSS selector")
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("Fill text must be at most 2000 characters")


def browser_setup(machine, workspace_id: str) -> dict:
    """Install Playwright/Chromium into this disposable workspace only."""
    command = (
        "python -m pip install -q playwright && "
        "sudo -n python -m playwright install-deps chromium >/dev/null 2>&1 && "
        "python -m playwright install chromium >/dev/null 2>&1 && "
        "(test -n \"$(sudo -n swapon --noheadings --show)\" || "
        "(sudo -n fallocate -l 512M /tmp/devmcp-browser.swap && "
        "sudo -n chmod 600 /tmp/devmcp-browser.swap && "
        "sudo -n mkswap /tmp/devmcp-browser.swap >/dev/null && "
        "sudo -n swapon /tmp/devmcp-browser.swap))"
    )
    result = machine.run(workspace_id, command, timeout_seconds=300)
    return {"installed": result["exit_code"] == 0, **result}


def browser_visit(machine, workspace_id: str, url: str, action: str = "inspect",
                  selector: str = "", value: str = "") -> dict:
    """Navigate, optionally click/fill an element, capture screenshot and extract visible text."""
    validate_browser_input(url, action, selector, value)
    payload = json.dumps({"url": url, "action": action, "selector": selector, "value": value})
    program = base64.b64encode(_BROWSER_RUNNER.encode()).decode()
    command = ("python -c " + shlex.quote("import base64;exec(base64.b64decode(" + repr(program) + "))")
               + " " + shlex.quote(payload))
    result = machine.run(workspace_id, command, timeout_seconds=65)
    if result["exit_code"] != 0:
        return {"ok": False, "error": result["stderr"][-2500:], "exit_code": result["exit_code"]}
    try:
        data = json.loads(result["stdout"].strip())
    except (ValueError, TypeError):
        return {"ok": False, "error": "Browser returned invalid output",
                "stdout": result["stdout"][-500:], "exit_code": result["exit_code"]}
    return data
