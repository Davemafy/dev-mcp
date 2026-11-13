# Native Vultr DevMCP — systemd + localhost SSH (no Docker)

The owner-facing MCP gateway and shell executor run directly on Ubuntu under a
dedicated **UID 10001** system account, managed by `systemd`.

**ChatGPT connects to MCP over HTTPS.** Raw SSH is used for host administration,
not as the MCP transport. There is no Railway command queue, polling worker,
or Docker container in the new service.

## Preservation and deployment

- The existing `/srv/devmcp-workspaces` directories remain in place.
  They were created as UID 10001, which the native service also uses.
- `/srv/devmcp-native-state` stores OAuth state and the dedicated SSH identity.
- During migration, do not write to the same workspace from both the old
  Railway worker and the new systemd service at once.
- Existing jobs do not migrate; files do. The new OAuth issuer re-signs
  all workspaces using `vm_workspace_recover`.

Point your chosen DevMCP hostname (example `devmcp.davemafy.xyz`) at Vultr's
public IPv4 address using a DNS A record. Then use the Vultr root console:

```bash
cd /opt/dev-mcp
git pull --ff-only origin main
sudo DEVMCP_DOMAIN=devmcp.davemafy.xyz bash deploy/bootstrap-vultr-native.sh
curl -fsS http://127.0.0.1:8742/health
```

The bootstrap installs Python, Playwright Chromium, fonts and FFmpeg on Ubuntu,
creates the unprivileged service account, sets up owner-authorized SSH and
starts `devmcp-native.service`. It does not run `docker`, recreate containers,
or modify /srv/devmcp-workspaces. It is safe to rerun and reuses existing OAuth
state and host SSH key.

The OAuth owner password is generated on first boot and stored in the private
state directory. Retrieve it from `sudo journalctl -u devmcp-native` on the
VM. **Never paste passwords or keys into a chat or commit them to Git.**

## HTTPS and ChatGPT

The native gateway listens only on `127.0.0.1:8742`. Add a new hostname
virtual host to the **existing** HTTPS reverse proxy (do not replace Refit's
routes). Example for an existing Caddy configuration:

```caddy
devmcp.davemafy.xyz {
    reverse_proxy 127.0.0.1:8742
}
```

If no reverse proxy is installed, configure one separately. Don't start
another proxy on TCP 443 if that port is already occupied.

Verify:

```bash
curl -fsS https://devmcp.davemafy.xyz/health
curl -fsS https://devmcp.davemafy.xyz/.well-known/oauth-authorization-server
```

Add `https://devmcp.davemafy.xyz/mcp` in ChatGPT and authenticate with the
new owner password. Verify `vm_status` has `mode=direct-vultr`, then
`vm_workspace_recover` returns the original workspaces. Run a read-only
test command, then verify that `vm_host_admin_status` authenticates over
pinned SSH to the local host.

Only after the new connection is verified, stop the legacy worker:

```bash
cd /opt/dev-mcp
sudo docker compose -f deploy/vm-worker.compose.yml stop devmcp-vm-worker
```

The **legacy stop command** mentions Docker only because that's how the old
worker was launched. The new service has no Docker dependency.

## Native service and raw SSH

`deploy/setup-native-host-ssh.sh` provisions a separate SSH identity owned
by UID 10001 and authorizes it for `devmcp-admin`. This account has
`sudo NOPASSWD:ALL`: **this is root-equivalent remote administration**.

SSH uses the independently installed local host Ed25519 key at
`/etc/ssh/ssh_host_ed25519_key.pub`, strict checking, batch-only
authentication and no SSH agent or port forwarding. No SSH secret is returned
through MCP. The service itself runs unprivileged, with writable access
limited to its own workspace/state directories.

The worker has no Docker CPU, RAM or PID quotas. The native service also has
no `CPUQuota`, `MemoryMax` or `TasksMax` ceiling. Real hardware and Linux
kernel limits still apply; workloads can consume enough RAM to trigger OOM.

Useful commands:

```bash
sudo systemctl status devmcp-native
sudo journalctl -u devmcp-native -n 60 --no-pager
sudo systemctl restart devmcp-native
```

## Rollback

```bash
sudo systemctl disable --now devmcp-native
cd /opt/dev-mcp
sudo docker compose -f deploy/vm-worker.compose.yml start devmcp-vm-worker
```

Neither action removes the workspaces. Do not tear down Railway until HTTPS,
OAuth, SSH administration and workspace recovery have been verified.

## Nginx HTTPS cutover on the existing VM

The native service runs on localhost:8742. Nginx already owns ports 80 and
443 for Refit, so it needs an **additional** hostname-specific vhost and
its own Let's Encrypt certificate.

Run on the existing Vultr console:

```bash
cd /opt/dev-mcp
git pull --ff-only origin main
sudo bash deploy/configure-native-nginx.sh devmcp.davemafy.xyz
```

The script preflights native DevMCP, verifies Nginx includes conf.d, refuses to
overwrite an unrelated config, configures an HTTP-01 challenge site, verifies
the public ACME challenge route, and obtains a certificate with Certbot
webroot. It then adds only a new SSL reverse proxy to localhost:8742, tests
Nginx before reloading, and checks the local and public HTTPS health endpoints.
It does not modify Refit's server block or reuse Refit's certificate.

If the public challenge check fails, confirm Cloudflare's origin A record and
temporarily switch this hostname to DNS-only during issuance if necessary.
Do not stop the legacy worker until OAuth and all four existing workspaces are
verified through the new endpoint.
