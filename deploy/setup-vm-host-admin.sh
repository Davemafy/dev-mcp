#!/usr/bin/env bash
# Owner-run once from the Vultr VM console. Creates a separate SSH admin user.
# This grants owner-authorized DevMCP root-equivalent access via sudo.
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then
  echo "Run: sudo bash deploy/setup-vm-host-admin.sh" >&2
  exit 1
fi
for bin in ssh-keygen useradd install visudo sshd; do
  command -v "$bin" >/dev/null || { echo "Missing $bin; install openssh-server and sudo." >&2; exit 1; }
done
if [[ ! -f /etc/ssh/ssh_host_ed25519_key.pub ]]; then
  echo "Missing the host Ed25519 public key; refusing unpinned SSH." >&2
  exit 1
fi
user=devmcp-admin
state=/etc/devmcp/host-admin
if ! id "$user" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash --user-group "$user"
  passwd -l "$user" >/dev/null
fi
install -d -m 0700 -o root -g root "$state"
if [[ -L $state/identity_ed25519 || -L $state/identity_ed25519.pub ]]; then
  echo "Unexpected SSH identity symlink; refusing." >&2
  exit 1
fi
if [[ ! -e $state/identity_ed25519 ]]; then
  ssh-keygen -q -t ed25519 -N '' -C devmcp-vultr-owner -f "$state/identity_ed25519"
fi
chmod 600 "$state/identity_ed25519"
chown root:root "$state/identity_ed25519"
pub="$(ssh-keygen -y -f "$state/identity_ed25519")"
hostpub="$(awk '$1=="ssh-ed25519" {print $1" "$2}' /etc/ssh/ssh_host_ed25519_key.pub)"
[[ -n "$hostpub" ]] || { echo "Host Ed25519 key missing." >&2; exit 1; }
printf 'host.docker.internal %s\n' "$hostpub" > "$state/known_hosts"
chmod 600 "$state/known_hosts"
chown root:root "$state/known_hosts"
home="$(getent passwd "$user" | cut -d: -f6)"
install -d -m 0700 -o "$user" -g "$user" "$home/.ssh"
printf 'restrict %s\n' "$pub" > "$home/.ssh/authorized_keys"
chown "$user:$user" "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"
printf '%s ALL=(root) NOPASSWD:ALL\n' "$user" > /etc/sudoers.d/devmcp-admin
chmod 0440 /etc/sudoers.d/devmcp-admin
visudo -cf /etc/sudoers.d/devmcp-admin >/dev/null
echo "DevMCP host SSH identity installed. Root-level sudo access has been granted to devmcp-admin."
echo "The private key stays on the host in $state and is never printed."
echo "Recreate ONLY the DevMCP worker to mount the key and apply updated resource limits."
