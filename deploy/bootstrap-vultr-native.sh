#!/usr/bin/env bash
# Install native FastMCP directly on Ubuntu with systemd. No Docker in this path.
# Preserve the existing /srv/devmcp-workspaces and running legacy worker.
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then echo 'Run with sudo.' >&2; exit 1; fi
cd "$(dirname "$0")/.."
domain="${DEVMCP_DOMAIN:-}"
if [[ ! "$domain" =~ ^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]]; then
  echo 'Set DEVMCP_DOMAIN to your existing DNS hostname (no protocol).' >&2
  exit 1
fi
if [[ ! -d /srv/devmcp-workspaces || -L /srv/devmcp-workspaces ]]; then
  echo 'Required workspace directory /srv/devmcp-workspaces is missing or a symlink. Refusing.' >&2
  exit 1
fi
if getent passwd 10001 >/dev/null; then
  [[ "$(getent passwd 10001 | cut -d: -f1)" == devmcp ]] || {
    echo 'UID 10001 is owned by another host user. Review before proceeding.' >&2
    exit 1
  }
else
  if getent group 10001 >/dev/null; then
    [[ "$(getent group 10001 | cut -d: -f1)" == devmcp ]] || {
      echo 'GID 10001 belongs to another host group. Review before proceeding.' >&2
      exit 1
    }
  else
    groupadd -g 10001 devmcp
  fi
  useradd -u 10001 -g 10001 -m -d /home/devmcp -s /usr/sbin/nologin devmcp
fi
# Change ONLY the parent directory's group/mode so the native UID 10001
# service can create new workspaces. Do not recursively change old project files.
chgrp devmcp /srv/devmcp-workspaces
chmod 2770 /srv/devmcp-workspaces
install -d -m 0700 -o devmcp -g devmcp /srv/devmcp-native-state
install -d -m 0700 -o root -g root /etc/devmcp
envfile=/etc/devmcp/native.env
if [[ -L $envfile ]]; then echo 'Refusing symlinked environment file.' >&2; exit 1; fi
if [[ ! -e $envfile ]]; then
  umask 077
  printf 'PUBLIC_BASE_URL=https://%s\n' "$domain" > "$envfile"
elif ! grep -qxF "PUBLIC_BASE_URL=https://$domain" "$envfile"; then
  echo "Existing $envfile has another OAuth hostname. Stop and review manually." >&2
  exit 1
fi
chmod 0600 "$envfile"
chown root:root "$envfile"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  python3 python3-pip python3-venv python3-dev git curl ca-certificates \
  openssh-client openssh-server sudo ffmpeg fonts-liberation
if [[ ! -x /opt/devmcp-python/bin/python ]]; then
  python3 -m venv /opt/devmcp-python
fi
/opt/devmcp-python/bin/python -m pip install --upgrade pip
/opt/devmcp-python/bin/python -m pip install -r requirements.txt \
  'playwright>=1.55,<2' 'pillow>=11,<13' 'numpy>=2,<3' 'trimesh>=4,<6'
install -d -m 0755 -o root -g root /opt/devmcp-browsers
PLAYWRIGHT_BROWSERS_PATH=/opt/devmcp-browsers \
  /opt/devmcp-python/bin/python -m playwright install --with-deps chromium-headless-shell
chmod -R a+rX /opt/devmcp-browsers /opt/devmcp-python

# Explicit authorization of root-equivalent SSH was requested for this machine.
bash deploy/setup-native-host-ssh.sh
install -m 0644 deploy/devmcp-native.service /etc/systemd/system/devmcp-native.service
systemctl daemon-reload
systemctl enable --now devmcp-native.service

echo 'Native DevMCP installed (NO Docker).'
systemctl --no-pager --full status devmcp-native.service || true
echo 'Verify locally: curl -fsS http://127.0.0.1:8742/health'
echo "Route HTTPS $domain to 127.0.0.1:8742 using existing reverse proxy."
echo 'Keep the old Railway and worker running until direct OAuth and workspace recovery pass.'
