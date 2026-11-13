# Direct Vultr deployment (no Railway gateway or polling worker)

This is the **single-owner** alternative to the Railway + outbound worker model.
DevMCP runs the authenticated MCP gateway and development commands in **one**
Docker service on the Vultr VM. The application is not the Docker host.

## Data preservation

- Existing `/srv/devmcp-workspaces` is mounted directly at `/workspaces`.
  Neither bootstrap nor the new compose file deletes or copies the workspaces.
- OAuth data is stored separately at `/srv/devmcp-direct-state`.
- Workspace handles are **reissued** via `vm_workspace_recover`: the new
  HTTPS endpoint has a different OAuth issuer/signing secret from Railway.
- The old worker continues running during staging. Stop it only after you
  verify the direct service, HTTPS, owner login and recovered workspaces.
- Existing shell processes and browser sessions are not transferred between
  gateway implementations; workspace files are retained.

## 1. Prepare DNS and deploy the *private* gateway

Choose an HTTPS hostname you control (example: `devmcp.davemafy.xyz`).
Create its DNS A record pointing to your Vultr IPv4 address.

From the Vultr console:

```bash
cd /opt/dev-mcp
git pull --ff-only origin main
sudo DEVMCP_DOMAIN=devmcp.davemafy.xyz bash deploy/bootstrap-vultr-direct.sh
curl -fsS http://127.0.0.1:8742/health
```

`PUBLIC_BASE_URL` goes in root-owned `/etc/devmcp/direct.env`.
Do **not** paste OAuth credentials or SSH keys into chat. A new owner password
is generated into the private container logs on first boot unless supplied
via `OWNER_PASSWORD` in the environment file. Generated credentials and
signing secret persist in the mounted state directory.

The service listens only at **127.0.0.1:8742**. No public port is exposed;
Refit's existing public services remain untouched.

## 2. Configure the public HTTPS reverse proxy

Point the hostname's DNS A record at the VM. Route HTTPS requests for the
new hostname to `127.0.0.1:8742` using the **existing** TLS proxy, if any.

For a host already running Nginx or Caddy, add a **new virtual host**, not
a replacement of Refit's configuration.

For example, a Caddy site block inside an *existing* Caddyfile:

```caddy
devmcp.davemafy.xyz {
    reverse_proxy 127.0.0.1:8742
}
```

If no reverse proxy owns TCP 443, use the optional Caddy installer (it does
not bind port 80):

```bash
sudo DEVMCP_DOMAIN=devmcp.davemafy.xyz bash deploy/start-vultr-direct-tls.sh
```

The installer refuses to hijack an occupied port 443. If another service
already owns 443, add a hostname route to that existing reverse proxy instead.
For Caddy's automatic certificates, the domain must resolve to this Vultr VM
and TCP 443 must be reachable from the Internet (TLS-ALPN validation).

Verify externally:

```bash
curl -fsS https://devmcp.davemafy.xyz/health
curl -fsS https://devmcp.davemafy.xyz/.well-known/oauth-authorization-server
```

`/mcp` must remain protected by the existing OAuth bearer gate. Never
publish the private loopback port without HTTPS. Rotate any exposed credentials.

## 3. Reconnect ChatGPT to direct DevMCP

Use `https://devmcp.davemafy.xyz/mcp` as the MCP connector endpoint. Sign
in with the private new owner password. Do not share it in a message.

Verify the new `vm_status` reports `mode: direct-vultr`, then recover and
inspect the old workspaces with `vm_workspace_recover`. Run one bounded
command and confirm data paths are unchanged.

The existing `vm_host_admin_run` tool uses the VM owner's separately
authorized SSH identity, mounted read-only into the gateway. It retains
host-key pinning, audits command digests, and does not expose the key
inside unprivileged workspace commands. This is root-equivalent access:
keep the gateway single-owner and protect its OAuth credentials.

## 4. Cut over / rollback

Only after HTTPS, OAuth, authenticated shell commands and workspace recovery
pass, stop the old worker:

```bash
cd /opt/dev-mcp
sudo docker compose -f deploy/vm-worker.compose.yml stop devmcp-vm-worker
```

Then retire Railway **manually** after confirming you no longer need its
OAuth database, active sessions, or other services.

Rollback is simply restarting the old worker. The two deployments share
the same workspace directory, so **do not run destructive write operations
concurrently against the same workspace**.

```bash
sudo docker compose -f deploy/vm-worker.compose.yml start devmcp-vm-worker
```

The direct gateway exposes no CPU/RAM/PID container throttle. Host physical
memory and Linux kernel limits still apply; exhausting RAM can still OOM.
