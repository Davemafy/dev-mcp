#!/usr/bin/env bash
# Start the direct gateway alongside the old worker. No cutover or destructive steps.
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then echo 'Run with sudo.' >&2; exit 1; fi
cd "$(dirname "$0")/.."
domain="${DEVMCP_DOMAIN:-}"
if [[ ! "$domain" =~ ^[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+$ ]]; then
  echo "Set a DNS hostname: sudo DEVMCP_DOMAIN=devmcp.example.com bash deploy/bootstrap-vultr-direct.sh" >&2
  exit 1
fi
if [[ ! -d /srv/devmcp-workspaces ]]; then
  echo "Missing /srv/devmcp-workspaces: refusing to start without persistent workspace data." >&2
  exit 1
fi
install -d -m 0700 -o root -g root /etc/devmcp /srv/devmcp-direct-state
install -d -m 0700 -o root -g root /etc/devmcp/host-admin
envfile=/etc/devmcp/direct.env
if [[ -L $envfile ]]; then echo 'Refusing symlinked env file' >&2; exit 1; fi
if [[ ! -f $envfile ]]; then
  umask 077
  printf 'PUBLIC_BASE_URL=https://%s\n' "$domain" > "$envfile"
else
  grep -qxF "PUBLIC_BASE_URL=https://$domain" "$envfile" || {
    echo "Existing $envfile uses another domain. Review it before changing OAuth issuer." >&2
    exit 1
  }
fi
chmod 0600 "$envfile"
chown root:root "$envfile"
docker compose -f deploy/vm-direct.compose.yml up -d --build --no-deps devmcp-direct
echo "Direct gateway now bound to 127.0.0.1:8742 (not publicly exposed)."
echo "Verify: curl -fsS http://127.0.0.1:8742/health"
echo "Configure HTTPS reverse proxy for https://$domain, then connect ChatGPT to https://$domain/mcp."
echo "Old Railway worker is intentionally STILL running; do not stop it until HTTPS/OAuth/workspace recovery pass."
