# dev-mcp

A private cloud development machine exposed through MCP. Railway hosts the Python FastMCP gateway; each workspace is an E2B Linux sandbox. The gateway does not execute arbitrary terminal commands itself.

## Deploy to Railway

1. Deploy this repository with the included Dockerfile.
2. Create an E2B account and set the Railway environment variables described in `.env.example`.
3. Set `E2B_API_KEY`, `OWNER_PASSWORD` (20+ characters), and `TOKEN_SIGNING_SECRET` (32+ characters) as Railway secrets. Do not commit their actual values.
4. Generate a Railway domain. `PUBLIC_BASE_URL` can be omitted if Railway supplies `RAILWAY_PUBLIC_DOMAIN`; otherwise set it to the HTTPS origin.
5. Optional: mount a Railway volume at `/data` and set `AUTH_DB_PATH=/data/dev-mcp-auth.sqlite` for OAuth one-time-code and refresh-token replay state across restarts.
6. Check `https://YOUR_DOMAIN/health`. MCP is served at `https://YOUR_DOMAIN/mcp` using Streamable HTTP; OAuth discovery is available at `/.well-known/oauth-authorization-server`.

## Capabilities

`workspace_create`, `workspace_status`, `workspace_close`, `write_file`, `read_file`, `list_files`, `run_terminal_command`, `start_process`, `list_processes`, `stop_process`, and `preview_url`.

Create one workspace, then reuse the returned signed `workspace_id` for every subsequent file and terminal call. E2B sandbox files survive across tool calls until the workspace expires or is closed.

The gateway uses single-owner OAuth with authorization code, PKCE, signed bearer tokens, dynamic client registration, and an owner-password approval page. Do not select anonymous/no-auth access for terminal tools.

## Test

Install dependencies from `requirements.txt` plus `pytest` and `httpx`, then run `pytest -q`. Local tests use a simulated E2B adapter, not E2B cloud. After deployment, verify an actual E2B sandbox: create workspace, write `hello.py`, execute `python3 hello.py`, read it back, close workspace.

**Unverified:** Railway deployment, real FastMCP handshake, live E2B execution, ChatGPT plugin installation. All require a deployed instance and E2B credentials.

## Boundaries

The E2B API key remains in Railway. Do not copy confidential credentials into disposable sandboxes. The sandbox can access the network; disposable does not mean completely risk-free. The local filesystem restrictions on file tools do not restrict terminal commands inside E2B. E2B charges and quotas apply.

Depending on your ChatGPT plan and workspace settings, write-enabled custom MCP connection may be unavailable. Test account eligibility before paying for infrastructure.

This repository is standalone; it is not part of Faultline.
