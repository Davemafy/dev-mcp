# dev-mcp

A private cloud development machine exposed through MCP. Railway hosts the Python FastMCP gateway; each workspace is an E2B Linux sandbox. The gateway does not execute arbitrary terminal commands itself.

## Deploy to Railway

1. Deploy this repository with the included Dockerfile.
2. Generate a public Railway domain. `PUBLIC_BASE_URL` can be omitted when Railway supplies `RAILWAY_PUBLIC_DOMAIN`; otherwise set it to your exact HTTPS origin.
3. Visit `https://YOUR_DOMAIN/health`. **The gateway starts even if no login variables or E2B key have been configured.** It returns `e2b_configured: false` until you configure E2B. Find your **automatically generated owner login password** in the private Railway deployment logs (search for `Initial owner login password`).
4. To make workspace creation work, add `E2B_API_KEY` as a Railway variable. A valid E2B account is required for actual Linux sandbox execution; don't put the key in a chat.
5. Optional for stable OAuth login after restarts: set `OWNER_PASSWORD` (20+ characters), `TOKEN_SIGNING_SECRET` (32+ characters), or attach a Railway volume at `/data` and set `AUTH_DB_PATH=/data/dev-mcp-auth.sqlite` so the generated credentials and replay database survive deployment.
6. MCP is served at `https://YOUR_DOMAIN/mcp` using Streamable HTTP; OAuth discovery is available at `/.well-known/oauth-authorization-server`. Login uses the owner password—not the E2B key.

### If the service starts but E2B doesn't work

`/health` returns HTTP 200 with `e2b_configured: false` when `E2B_API_KEY` is absent. This is intentionally not a crash. The workspace tools return a descriptive error until the key is set. The password can change on Railway restarts if you don't supply explicit credentials or a persistent volume. Never expose your generated login password or sandbox key publicly.

## Capabilities

`workspace_create`, `workspace_status`, `workspace_close`, `write_file`, `read_file`, `list_files`, `run_terminal_command`, `start_process`, `list_processes`, `stop_process`, `preview_url`, `browser_setup`, and `browser_visit`.

Create one workspace, then reuse the returned signed `workspace_id` for every subsequent file and terminal call. E2B sandbox files survive across tool calls until the workspace expires or is closed.

### Recover an existing workspace after losing its handle

Call the existing `workspace_status` tool with `workspace_id: "recover"`.
The authenticated owner receives a bounded inventory of surviving running or
paused workspaces from the configured E2B project, with fresh signed handles.
Select one of those handles for the normal status, file and terminal tools.
This discovery operation does not create, resume or execute anything in a
workspace. Paused machines resume only when explicitly reconnected.

New workspaces carry an installation tag; recovery excludes machines tagged
for another dev-mcp installation. Untagged machines created by older versions
are shown explicitly as legacy project candidates. Arbitrary E2B IDs and the
`recover` keyword cannot authorize terminal access or deletion: those operations
still require a valid signed handle. The inventory is available only behind
the same single-owner OAuth gate, never from public `/health`.

Recovery cannot restore the files of an expired or deleted sandbox. It returns
an empty inventory when none survive and never replaces a missing machine with
a fresh empty one. `/health` includes `workspace_recovery_version: 1` after this
feature is deployed. Existing tool clients do not need a new tool schema.

The gateway uses single-owner OAuth with authorization code, PKCE, signed bearer tokens, dynamic client registration, and an owner-password approval page. Do not select anonymous/no-auth access for terminal tools.

## Test

Install dependencies from `requirements.txt` plus `pytest` and `httpx`, then run `pytest -q`. Local tests use a simulated E2B adapter, not E2B cloud. After deployment, verify an actual E2B sandbox: create workspace, write `hello.py`, execute `python3 hello.py`, read it back, close workspace.

**Unverified:** Railway deployment, real FastMCP handshake, live E2B execution, ChatGPT plugin installation. All require a deployed instance and E2B credentials.

## Boundaries

The E2B API key remains in Railway. Do not copy confidential credentials into disposable sandboxes. The sandbox can access the network; disposable does not mean completely risk-free. The local filesystem restrictions on file tools do not restrict terminal commands inside E2B. E2B charges and quotas apply.

Depending on your ChatGPT plan and workspace settings, write-enabled custom MCP connection may be unavailable. Test account eligibility before paying for infrastructure.

This repository is standalone; it is not part of Faultline.

## Browser automation (self-extension demo)

Create a workspace, call `browser_setup(workspace_id)` to install Playwright/Chromium
inside the E2B sandbox, then `browser_visit(workspace_id, "https://example.com")`.
The browser tool returns rendered text, title, links, status and a screenshot path
inside the same workspace. You can also use `action="click"` or `action="fill"`
with a CSS selector; these actions operate on the loaded page.

The browser runs in E2B, **not on Railway**. Browser navigation can reach arbitrary
sites accessible from that workspace, so treat the tool as privileged and do not
expose the owner-authenticated gateway to untrusted users. The screenshots remain
in the E2B machine and are not directly attached to chat. Every new disposable
workspace requires a new browser installation.


## Experimental Work-style bridge (branch: feat/browser-railway-bridge)

This branch adds four MCP tools to the existing owner-authenticated gateway:

- `browser_journey(workspace_id, url, steps, viewport)` runs up to 15 sequential Playwright actions in **one** Chromium browser context, for mobile or desktop, and records the screenshot path, visible content, failed requests, and JavaScript errors.
- `browser_capture(workspace_id)` returns that screenshot as MCP **ImageContent** (not merely a filename) so an assistant with image-capable tool access can inspect the pixels. It only reads the fixed capture file inside the signed E2B workspace.
- `railway_deployments(limit)` lists status/IDs for a single configured Railway service.
- `railway_logs(deployment_id, kind, limit)` provides sanitized read-only build/runtime logs, verifying that the requested deployment belongs to the configured service.

The current deployed MCP connection will not expose newly registered tools until this branch is deployed and the client refreshes its tool discovery. Installing code alone does not refresh the ChatGPT connector.

### Railway diagnostics configuration

Set these as **Dev MCP gateway** environment variables in Railway, not inside an E2B sandbox and not in chat:

```dotenv
RAILWAY_PROJECT_TOKEN=...    # project-scoped token, preferred
RAILWAY_PROJECT_ID=...
RAILWAY_SERVICE_ID=...
RAILWAY_ENVIRONMENT_ID=...
```

Alternatively use `RAILWAY_API_TOKEN` (account/workspace token) but prefer the least-privileged project token. The integration only sends GraphQL **queries**, never mutations. The project token uses Railway's `Project-Access-Token` header. Logs are bounded and known sensitive patterns are redacted; because arbitrary app output may leak secrets in novel formats, do not treat the redaction as an absolute guarantee.

### Browser testing

1. Create or recover a signed E2B workspace.
2. Call `browser_setup` once (installs Playwright/Chromium into that sandbox).
3. Call `browser_journey` with an HTTPS URL and a sequence like `[{"action":"fill","selector":"input","value":"hello"},{"action":"click","selector":"button"},{"action":"assert_text","selector":"body","value":"hello"}]`.
4. Call `browser_capture` to view the final screenshot.

Supported step actions: `click`, `fill`, `press`, `wait_for`, `assert_text`, `scroll`. Each step is validated and bounded. Actions execute inside E2B, not in the gateway. The screenshot is a compressed viewport JPEG; it is not a full-page screenshot. The browser context survives across the steps of **one journey**, not across separate calls; use one journey for one continuous interaction.

### What's not yet implemented

A multi-hour, chat-independent engineering worker needs persistent job storage, a worker scheduler, controlled retries and resumable checkpoints. It is not supplied by an E2B terminal background process alone. A proposed implementation is a persistent Railway worker with a durable queue and short-lived, signed E2B workspaces; make job cancellation and budget controls part of its initial design. Do not advertise this branch as a running autonomous agent.

### Verification

The E2B test run completed `python -m pytest -q` with **37 passed** and `python -m compileall` without syntax errors. These are mocked-unit tests, not a live Railway token query or a real Chromium acceptance run. Enable the appropriate gateway variables and run live smoke tests only after deployment and tool discovery have refreshed.


## Cloud-streaming browser (experimental branch)

The gateway supports a **persistent Chromium page** shared between visual human control and agent calls. Unlike \`browser_journey\`, it does not discard the browsing context after every action.

### Browser workflow

1. Install Playwright and Chromium once using \`browser_setup(workspace_id)\`. On small E2B machines, swap is necessary; the existing installer provisions it.
2. Call \`browser_stream_start(workspace_id, "https://example.com", "mobile")\`.
3. Open the returned **private** \`view_url\`. The page streams compressed JPEG frames over HTTPS and accepts address navigation, clicks, typing, scroll and keyboard actions.
4. Use \`browser_stream_action\` to act on the *same page* from ChatGPT. Use \`browser_stream_status\` for current URL/title and \`browser_capture\` for a screenshot delivered to the assistant as an image.
5. Call \`browser_stream_stop\` or close the E2B workspace to invalidate its browser session.

The viewer link is a bearer capability. Keep it private. The gateway signs a **30-minute browser-view ticket** containing a fixed sandbox identifier, but neither an account API key nor E2B's traffic access token. A separate one-time-in-the-URL-fragment capability exchanges for a Secure, HttpOnly, SameSite cookie on the viewer. Both protections are required.

### Why the reverse proxy exists

E2B sandboxes may reject external HTTP requests without an \`e2b-traffic-access-token\` request header. Normal browser navigation doesn't supply this header. Dev MCP's Railway gateway exposes only \`/browser/{signed-ticket}/...\`, validates the ticket, fixes the sandbox port to 3487, and injects the E2B token *server-side*. It also blocks cross-site POSTs. All sandbox ports remain behind E2B's native traffic gate.

The viewer is a low-frame-rate multipart JPEG stream, **not a 60 fps VNC desktop**. While the stream is open, frames update roughly once a second. Idle frames are captured at a lower rate to reduce memory pressure. The E2B machine must remain alive; a Railway deployment alone does not keep an E2B workspace running indefinitely.

### Verification and limits

- Local tests in E2B: \`python -m pytest -q\` — 45 passed.
- Real E2B Chrome acceptance: Chromium opened \`https://example.com\`, clicked through to the linked page, navigated back, accepted keyboard input and scrolled in the same session.
- Local authenticated stream accepted requests with a bearer capability and returned actual multipart JPEG frames; unauthenticated requests returned HTTP 401.
- An E2B preview URL *without* the traffic header returned HTTP 403, as expected; the new Railway reverse proxy is tested with a mocked upstream but still needs deployment and external-browser verification.
- The experimental browser does not implement audio streaming, an entire desktop/window manager, durable multi-hour jobs, or a guarantee of uptime after an E2B workspace expires.

## Experimental full remote desktop

The separate `feat/desktop-adaptive-runtime` branch adds a real Xvfb/Fluxbox/noVNC desktop, a protected WebSocket gateway, visible Chromium on that desktop, model-written adaptive Playwright actions, loopback-only CDP script attachment, and an optional MCP Apps viewer resource. See [the desktop engineering guide](docs/desktop-runtime.md) for setup, constraints, security and verification receipts. This is **not yet deployed**.
