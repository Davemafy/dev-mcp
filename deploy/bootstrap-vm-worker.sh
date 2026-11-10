#!/usr/bin/env bash
# One-time bootstrap on the user's Vultr VM. Run from the dev-mcp repo root.
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "Run as root through the Vultr console: sudo bash deploy/bootstrap-vm-worker.sh" >&2
  exit 1
fi

if ! command -v docker >/dev/null; then
  echo "Docker is required; existing Refit Docker services are not modified." >&2
  exit 1
fi

envfile=/etc/devmcp/vm-worker.env
if [[ ! -f "$envfile" || -L "$envfile" ]]; then
  echo "First create /etc/devmcp/vm-worker.env (root-owned 0600) with VM_GATEWAY_URL and VM_WORKER_TOKEN." >&2
  exit 1
fi

chmod 600 "$envfile"
chown root:root "$envfile"
if ! grep -Eq '^VM_GATEWAY_URL=https://[^[:space:]]+$' "$envfile"; then
  echo "VM_GATEWAY_URL must be an HTTPS origin." >&2
  exit 1
fi
if ! grep -Eq '^VM_WORKER_TOKEN=[A-Za-z0-9_+=/-]{32,}$' "$envfile"; then
  echo "VM_WORKER_TOKEN must be a long random token identical to the Railway secret." >&2
  exit 1
fi

install -d -m 0755 -o root -g root /srv/devmcp-workspaces
docker compose -f deploy/vm-worker.compose.yml up -d --build
docker compose -f deploy/vm-worker.compose.yml ps
echo "Worker container started. Verify vm_link.worker_connected in Railway /health."
echo "No SSH keys, root shell, or new inbound ports were configured."
