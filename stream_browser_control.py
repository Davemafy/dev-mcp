"""Local-only proxy for the owner-authenticated MCP browser stream controls.

Reads the stream token from the workspace file and never prints it.
"""
import base64
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

config = json.loads(Path("/home/user/.devmcp-stream/config.json").read_text())
token = config["token"]
status = len(sys.argv) <= 1 or sys.argv[1] == "--status"
url = "http://127.0.0.1:3487/" + ("state" if status else "control")
data = None if status else base64.b64decode(sys.argv[1], validate=True)
headers = {"Authorization": "Bearer " + token}
if data is not None:
    headers["Content-Type"] = "application/json"
request = Request(url, data=data, headers=headers, method="GET" if status else "POST")
try:
    with urlopen(request, timeout=26) as response:
        body = response.read(10000).decode("utf-8")
        print(body)
except HTTPError as error:
    try:
        parsed = json.loads(error.read(2000))
        parsed["http_status"] = error.code
        print(json.dumps(parsed))
    except Exception:
        print(json.dumps({"ok": False, "http_status": error.code}))
except (URLError, TimeoutError, ConnectionError, OSError):
    print(json.dumps({"ok": False, "error": "Browser service unavailable"}))
