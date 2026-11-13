#!/usr/bin/env bash
# Optional HTTPS setup. Does not replace or reconfigure existing web services.
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then echo 'Run with sudo.' >&2; exit 1; fi
cd "$(dirname "$0")/.."
domain="${DEVMCP_DOMAIN:-}"
if [[ ! "$domain" =~ ^[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+$ ]]; then
  echo "Example: sudo DEVMCP_DOMAIN=devmcp.davemafy.xyz bash deploy/start-vultr-direct-tls.sh" >&2
  exit 1
fi
if [[ ! -f /etc/devmcp/direct.env ]] ||
   ! grep -qxF "PUBLIC_BASE_URL=https://$domain" /etc/devmcp/direct.env; then
  echo "Direct gateway is not configured for https://$domain. Run bootstrap-vultr-direct.sh first." >&2
  exit 1
fi
if ! curl -fsS --max-time 3 http://127.0.0.1:8742/health >/dev/null; then
  echo "Direct MCP gateway is not answering on localhost:8742." >&2
  exit 1
fi
# Refuse to hijack port 443 used by an existing site or reverse proxy.
if command -v ss >/dev/null && ss -H -lnt '( sport = :443 )' | grep -q .; then
  if [[ -z "$(docker ps --filter name='^/devmcp-direct-tls$' --format '{{.Names}}')" ]]; then
    echo "Port 443 is already occupied. Add the DevMCP virtual host to your existing HTTPS proxy instead." >&2
    exit 1
  fi
fi
install -d -m 0700 -o root -g root /srv/devmcp-caddy-data /srv/devmcp-caddy-config
DEVMCP_DOMAIN="$domain" docker compose -f deploy/vm-direct-tls.compose.yml up -d
echo "Caddy started; check DNS A record and HTTPS at https://$domain/health."
echo "Port 80 was not touched. Existing Railway worker remains running."
