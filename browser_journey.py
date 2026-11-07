"""One-request, multi-step Playwright journey inside an authenticated E2B sandbox.

The MCP gateway forwards validated steps to the sandbox. Screenshots can be
retrieved separately as an MCP image by browser_capture.
"""
from __future__ import annotations

import base64
import json
import shlex
import zlib

from browser import validate_browser_input

MAX_STEPS = 15
ACTIONS = {"click", "fill", "press", "wait_for", "assert_text", "scroll"}

_RUNNER = r'''
import json, os, sys
from playwright.sync_api import sync_playwright

payload = json.loads(sys.argv[1])
steps = payload["steps"]
viewport = payload["viewport"]
screens = "/home/user/browser-captures"
os.makedirs(screens, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(
        viewport={"width": 390, "height": 844} if viewport == "mobile" else
                 {"width": 1440, "height": 900},
        device_scale_factor=1,
        is_mobile=viewport == "mobile",
        has_touch=viewport == "mobile",
    )
    page = context.new_page()
    console_errors = []
    failed_requests = []
    page.on("pageerror", lambda error: console_errors.append(str(error)[:300]))
    page.on("requestfailed", lambda request: failed_requests.append(request.url[:250]))
    records = []
    failure = None
    try:
        response = page.goto(payload["url"], wait_until="domcontentloaded", timeout=20000)
        for index, step in enumerate(steps):
            action = step["action"]
            selector = step.get("selector") or ""
            value = step.get("value") or ""
            try:
                locator = page.locator(selector).first if selector else None
                if action == "click":
                    locator.click(timeout=7500)
                elif action == "fill":
                    locator.fill(value, timeout=7500)
                elif action == "press":
                    locator.press(value, timeout=7500)
                elif action == "wait_for":
                    locator.wait_for(state="visible", timeout=7500)
                elif action == "assert_text":
                    if value.casefold() not in locator.inner_text(timeout=7000).casefold():
                        raise AssertionError("Expected text not present at selector")
                elif action == "scroll":
                    locator.scroll_into_view_if_needed(timeout=7000)
                records.append({"step": index + 1, "action": action, "ok": True})
            except Exception as error:
                failure = {"step": index + 1, "action": action,
                           "reason": type(error).__name__}
                records.append({"step": index + 1, "action": action, "ok": False})
                break
        page.wait_for_timeout(150)
        capture = screens + "/latest.jpg"
        page.screenshot(path=capture, type="jpeg", quality=65, full_page=False, timeout=10000)
        print(json.dumps({
            "ok": failure is None, "failed_step": failure,
            "url": page.url, "title": page.title(),
            "status": response.status if response else None,
            "viewport": viewport, "steps": records,
            "text": page.locator("body").inner_text(timeout=5000)[:7000],
            "console_errors": console_errors[-12:],
            "failed_requests": failed_requests[-12:],
            "screenshot_path": capture,
        }))
    finally:
        context.close()
        browser.close()
'''


def validate_journey(url: str, steps: list, viewport: str) -> None:
    validate_browser_input(url, "inspect", "", "")
    if viewport not in ("mobile", "desktop"):
        raise ValueError("Viewport must be mobile or desktop.")
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise ValueError("At most 15 browser actions are permitted.")
    for step in steps:
        if not isinstance(step, dict) or step.get("action") not in ACTIONS:
            raise ValueError("Unsupported browser action.")
        action = step["action"]
        selector = step.get("selector", "")
        value = step.get("value", "")
        validate_browser_input(url, "inspect", selector, value)
        if not selector or not isinstance(selector, str):
            raise ValueError("Every browser action requires a CSS selector.")
        if action in ("fill", "press", "assert_text") and (not isinstance(value, str) or not value):
            raise ValueError(action + " requires a value.")
    if len(json.dumps(steps)) > 4000:
        raise ValueError("Browser step payload is too large.")


def browser_journey(machine, workspace_id: str, url: str, steps: list,
                    viewport: str = "mobile") -> dict:
    validate_journey(url, steps, viewport)
    payload = json.dumps({"url": url, "steps": steps, "viewport": viewport})
    encoded = base64.b64encode(zlib.compress(_RUNNER.encode(), level=9)).decode()
    program = "import base64,zlib;exec(zlib.decompress(base64.b64decode(" + repr(encoded) + ")))"
    command = "python -c " + shlex.quote(program) + " " + shlex.quote(payload)
    result = machine.run(workspace_id, command, timeout_seconds=100)
    if result["exit_code"] != 0:
        return {"ok": False, "error": "Browser journey failed in sandbox",
                "error_type": "browser-process", "exit_code": result["exit_code"],
                "stderr": result["stderr"][-600:]}
    try:
        return json.loads(result["stdout"].strip())
    except (ValueError, TypeError):
        return {"ok": False, "error": "Invalid browser journey result"}
