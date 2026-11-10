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


## Persistent, restricted SSH access to Refit's Vultr VM

The gateway now includes three **owner-authenticated** MCP tools:
\`host_access_prepare(expected_fingerprint)\`, \`host_access_status()\`,
and \`host_access_deploy(revision)\`. Unlike \`run_terminal_command\`, these
operate through the Railway gateway, not a disposable E2B workspace.
The connector is disabled unless a **real persistent Railway volume** is
mounted at \`/data\`. The gateway automatically uses
\`/data/refit-host-ssh\`; the optional Railway variable
\`HOST_SSH_STATE_DIR\` can choose another subdirectory of that mount.
The directory must
remain on the volume across Railway deployments. Without that configuration,
the tools fail closed rather than quietly generate ephemeral credentials.

**Security:** the host is fixed at \`80.240.30.92\`, user \`root\`, port 22.
The first call requires the server's **independently verified** Ed25519 SHA256
fingerprint from the trusted Vultr console:
\`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub\`.
The connector probes the remote SSH host and refuses mismatches. It generates
an Ed25519 *client* private key under the Railway volume with owner-only
permissions; it never returns or logs that private key. The returned public
key is safe to paste in Vultr's console.

Use the generated \`host_setup\` instruction on the Vultr VM. It pulls the
verified Refit repository checkout and runs
\`deploy/setup-devmcp-host-access.sh\` with the **public** key. The helper
installs a root-owned SSH forced-command handler and appends a
\`restrict,expiry-time=...,command=...\` authorization lasting five hours.
It replaces only the dev-MCP authorization, preserving other SSH keys and
current Refit service budgets. There is no general-purpose SSH shell,
port forwarding, arbitrary SSH hostname, private-key upload or arbitrary
remote command parameter.

After installing the public key, invoke \`host_access_status()\` to verify
authentication and start the gateway's conservative local five-hour timer.
\`host_access_deploy(revision)\` accepts only a lowercase, full 40-character
Git commit SHA; the host additionally checks \`origin/main\`, clean
tracked files, gVisor readiness, deploys and external HTTPS revision
acceptance. On expiration call \`host_access_prepare\` again with the same
host fingerprint and re-run the printed public-key authorization command.
The *same* persisted client private key is reused; no new secret needs
copying. This five-hour authorization cannot renew itself remotely.

**Limitations:** pushing this repository does not prove Railway's deployment,
persistent volume or outbound SSH works. Confirm \`/health\` reports \`host_access_version: 1\`,
\`host_access_volume_mounted: true\` and \`host_access_configured: true\`,
then verify
\`host_access_status()\` against the live VM before claiming access.
The trusted host fingerprint must come from Vultr, not merely a network
scan of the host being connected to. The Railway volume holds a
high-value restricted SSH credential; protect owner OAuth credentials,
Railway access and the volume. Do not expose this connector to other users.


### Railway volume permissions

Railway volumes are initially root-owned. The production Docker entrypoint
changes ownership of the mounted \`/data\` directory to UID 10001, then drops
root privileges before starting the MCP/OAuth gateway. The running API and
SSH client therefore stay non-root. CI runs a real Docker container with a
volume, verifies a persistent file after restart, and verifies the gateway
UID is 10001. Do **not** set \`RAILWAY_RUN_UID=10001\` as an overriding
Railway deployment variable; doing so would prevent the one-time mount
ownership setup. Existing Railway auth secrets should remain unchanged.


## Portable gateway (Vercel, Railway, or any Python ASGI host)

The gateway runs on any ASGI-compatible host. It does not require a specific
execution provider. To deploy to Vercel use the root `app.py` ASGI entrypoint.
The existing Docker/Railway service remains compatible and unchanged by default.

**Vercel preparation**

1. Import this repository as a Vercel project using the **Python** runtime.
2. Set `PUBLIC_BASE_URL=https://<your-production-domain>` (no `/mcp`).
3. Set `OWNER_PASSWORD` (at least 20 characters) and
   `TOKEN_SIGNING_SECRET` (at least 32 characters), both stable across deploys.
4. Provision a managed **TLS Redis** instance and set
   `AUTH_REDIS_URL=rediss://...`. Stateless instances share single-use OAuth
   authorization and refresh-token replay protection and login attempt limits.
5. Set `WORKSPACE_BACKEND=e2b` and `E2B_API_KEY`. Do not place secrets in Git.
6. Deploy, verify `/health`, then connect your MCP client using `/mcp`.
   OAuth discovery remains under `/.well-known/` on the same origin.

The root `app.py` enables `STATELESS_GATEWAY=1`. Redis and explicit stable
credentials are **required** in this mode; startup fails closed otherwise.
For local single-instance operation, `main.py` still uses SQLite by default.

**Provider switching**

- `WORKSPACE_BACKEND=e2b` uses the existing disposable E2B machines.
- `WORKSPACE_BACKEND=worker` delegates to an owner-operated, independently
  sandboxed HTTPS worker with `WORKER_API_URL` and a 32+ character
  `WORKER_API_TOKEN`. The gateway adapter speaks POST `/v1/execute` with
  JSON `{"action":"create","args":{}}`; the worker must return a
  `{"worker_id":"..."}`. Other supported actions are `status`, `close`,
  `read`, `write`, `list`, `run`, `start`, `processes`, `stop`,
  `preview`. Workspace handles are signed and scoped to the worker backend.

**Security boundary:** the HTTP worker contract is an adapter, not an
automatically installed Vultr service. It must run with least privilege behind
HTTPS, with process and filesystem isolation and without sensitive host access.
Never point it at the production Refit machine or accept arbitrary tenants.
Remote job execution must not occur in the Vercel gateway itself.

**Deployment status:** GitHub code and local unit tests do not prove a working
Vercel deployment. Verify production OAuth discovery, MCP initialization and
real backend execution before switching clients away from Railway.

For a gateway SSH timeout, see [workspace SSH fallback](docs/workspace-host-access.md).

## Persistent Vultr worker (outbound-only)

DevMCP can operate both disposable E2B machines and a persistent, resource-limited
Vultr development container **without** a second model, expiring Refit SSH keys,
or a public VM port. The Railway gateway queues owner-authenticated VM commands
on its existing mounted `/data` volume; a dedicated worker on Vultr pulls
them over authenticated HTTPS.

Enable by setting `VM_WORKER_TOKEN` in Railway and installing the Docker worker
once on Vultr. See [complete setup and acceptance checks](docs/persistent-vultr-worker.md).
When disconnected, VM commands are rejected rather than queued for surprise execution.
Existing E2B tools and Refit host restrictions remain unchanged.

The VM code and queue can be tested locally, but the connection is **not live**
until the VM container and Railway token are installed and a live round-trip succeeds.
