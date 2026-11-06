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
