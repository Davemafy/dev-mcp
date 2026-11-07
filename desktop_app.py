"""Standards-first MCP App view for a signed, owner-only remote desktop.

The UI remains optional: tools work without a compatible MCP Apps host.
No browser credentials are stored in widget state.
"""
from __future__ import annotations

import json

RESOURCE_URI = "ui://devmcp/desktop-viewer-v1.html"


def desktop_app_html(gateway_base_url: str) -> str:
    origin = json.dumps(gateway_base_url.rstrip("/")).replace("<", "\\u003c")
    return r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dev MCP desktop viewer</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,-apple-system,sans-serif}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:transparent;color:CanvasText}
section{display:grid;gap:14px}
header{display:flex;gap:8px;align-items:center;justify-content:space-between}
h1{font-size:16px;line-height:1.25;font-weight:650;letter-spacing:-.02em;margin:0}
small{font-size:12px;color:GrayText}
button,a.action{display:inline-flex;align-items:center;justify-content:center;
min-height:42px;border:1px solid color-mix(in srgb,CanvasText 16%, transparent);
border-radius:12px;padding:9px 14px;background:Canvas;color:CanvasText;
font:inherit;font-size:13px;font-weight:570;text-decoration:none;cursor:pointer}
button:focus-visible,a:focus-visible{outline:2px solid #408761;outline-offset:3px}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.viewport{width:100%;height:min(72vh,650px);overflow:hidden;
background:color-mix(in srgb,CanvasText 6%,Canvas);border-radius:12px}
iframe{display:block;width:100%;height:100%;border:0}
#message{font-size:13px;line-height:1.5;color:GrayText;margin:0}
[hidden]{display:none!important}
</style>
</head>
<body>
<section>
<header><div><h1>Remote desktop</h1><small>Private live E2B workspace</small></div></header>
<p id="message">Start your desktop with Dev MCP to view it here.</p>
<div class="actions" id="actions" hidden>
<button id="load" type="button">Show live desktop</button>
<a id="external" class="action" rel="noopener noreferrer" target="_blank">Open separately</a>
</div>
<div class="viewport" id="container" hidden>
<iframe id="viewer" title="Live remote desktop" referrerpolicy="no-referrer"
sandbox="allow-scripts allow-same-origin allow-forms" allow="clipboard-read; clipboard-write"></iframe>
</div>
</section>
<script>
const gateway = __GATEWAY_ORIGIN__;
let viewerUrl = null;
const load = document.getElementById("load");
const external = document.getElementById("external");
const message = document.getElementById("message");
const container = document.getElementById("container");
const iframe = document.getElementById("viewer");
function useResult(value) {
  if (typeof value === "string") {
    try { value = JSON.parse(value); } catch (_) { return; }
  }
  const result = value?.result || value;
  const input = result?.viewer_url;
  if (typeof input !== "string") return;
  let u;
  try { u = new URL(input); } catch (_) { return; }
  if (u.origin !== gateway || !u.pathname.startsWith("/desktop/") ||
      u.protocol !== "https:" || u.username || u.password) return;
  viewerUrl = u.href;
  external.href = viewerUrl;
  document.getElementById("actions").hidden = false;
  message.textContent = "A shared, controllable desktop. Keep this session private.";
}
window.addEventListener("message", (event) => {
  if (event.source !== window.parent) return;
  const incoming = event.data;
  if (!incoming || incoming.jsonrpc !== "2.0") return;
  if (incoming.method === "ui/notifications/tool-result") {
    const params = incoming.params || {};
    useResult(params.structuredContent);
    if (!viewerUrl && Array.isArray(params.content)) {
      for (const item of params.content) {
        if (item.type === "text") useResult(item.text);
      }
    }
  }
});
load.addEventListener("click", () => {
  if (!viewerUrl) return;
  iframe.src = viewerUrl;
  container.hidden = false;
  load.textContent = "Reconnect desktop";
});
</script>
</body>
</html>""".replace("__GATEWAY_ORIGIN__", origin)
