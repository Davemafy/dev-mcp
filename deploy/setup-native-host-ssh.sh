#!/usr/bin/env bash
# Owner-authorized root-equivalent SSH from native DevMCP to localhost.
# Never copies or prints a private key; does not modify unrelated SSH accounts.
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then echo 'Run as root via sudo.' >&2; exit 1; fi

user=devmcp-admin
state=/srv/devmcp-native-state/ssh
for program in ssh-keygen sshd visudo; do
  command -v "$program" >/dev/null || { echo "Missing $program" >&2; exit 1; }
done
[[ -f /etc/ssh/ssh_host_ed25519_key.pub ]] || {
  echo 'Missing Ed25519 SSH host key. Inspect sshd setup.' >&2
  exit 1
}
if ! getent passwd "$user" >/dev/null; then
  useradd --create-home --shell /bin/bash --user-group "$user"
  passwd -l "$user" >/dev/null
fi
install -d -m 0700 -o devmcp -g devmcp "$state"
if [[ -L "$state/identity_ed25519" || -L "$state/known_hosts" ]]; then
  echo 'Unexpected SSH state symlink; refusing.' >&2; exit 1
fi
if [[ ! -f "$state/identity_ed25519" ]]; then
  runuser -u devmcp -- ssh-keygen -q -t ed25519 -N '' -C devmcp-native-owner -f "$state/identity_ed25519"
fi
chown devmcp:devmcp "$state/identity_ed25519"
chmod 0600 "$state/identity_ed25519"
pub="$(runuser -u devmcp -- ssh-keygen -y -f "$state/identity_ed25519")"
host_key="$(awk '$1=="ssh-ed25519" {print $1" "$2}' /etc/ssh/ssh_host_ed25519_key.pub)"
[[ -n "$host_key" ]] || { echo 'Host Ed25519 public key unavailable.' >&2; exit 1; }
printf '127.0.0.1 %s\n' "$host_key" > "$state/known_hosts"
chown devmcp:devmcp "$state/known_hosts"
chmod 0600 "$state/known_hosts"
home="$(getent passwd "$user" | cut -d: -f6)"
group="$(id -gn "$user")"
install -d -m 0700 -o "$user" -g "$group" "$home/.ssh"
auth="$home/.ssh/authorized_keys"
if [[ -L "$auth" ]]; then echo 'Unexpected authorized_keys symlink.' >&2; exit 1; fi
touch "$auth"
if ! grep -Fq -- "$pub" "$auth"; then printf 'restrict %s\n' "$pub" >> "$auth"; fi
chown "$user:$group" "$auth"
chmod 0600 "$auth"
sudo_rule=/etc/sudoers.d/devmcp-admin
if [[ -e $sudo_rule && ! -f $sudo_rule ]]; then
  echo 'Unexpected sudoers path; refusing.' >&2; exit 1
fi
printf '%s ALL=(root) NOPASSWD:ALL\n' "$user" > "$sudo_rule"
chmod 0440 "$sudo_rule"
visudo -cf "$sudo_rule" >/dev/null
echo 'Authorized native DevMCP SSH account (root-equivalent).'
echo 'Key stays in /srv/devmcp-native-state/ssh and is never printed.'
