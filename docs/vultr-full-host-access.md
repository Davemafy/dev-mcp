# Full VM capacity and persistent SSH administration

This configuration is intended for the **single-owner Vultr development VM**.
It is not safe for public/multi-tenant execution of untrusted commands.

## Capacity

The worker's Compose file no longer caps CPU or RAM. On an 8-vCPU/16-GB VM,
the worker can use all available capacity, subject to host OS and other
services. It is not guaranteed 16 GB of *free* memory; an OOM can still occur.
The worker's default PID ceiling is 2048. Container filesystem remains
read-only; source files live in /srv/devmcp-workspaces.

## One-time authorization on Vultr

The owner must explicitly choose to grant remote host administration.
This is a **root-equivalent** permission: the DevMCP gateway's owner account
can run arbitrary commands through its authenticated MCP tools. Do not enable
this on shared production hosts or for untrusted agents.

From the Vultr console, in the existing /opt/dev-mcp checkout:

```bash
cd /opt/dev-mcp
git fetch origin main
git switch main
git pull --ff-only origin main
sudo bash deploy/setup-vm-host-admin.sh
sudo docker compose -f deploy/vm-worker.compose.yml up -d --build --no-deps devmcp-vm-worker
```

The setup script creates a locked-password `devmcp-admin` account with a
dedicated SSH key and `sudo NOPASSWD:ALL`. **This explicitly grants
root-equivalent access.** Only the root-owned SSH private key is mounted into
the worker at /etc/devmcp/host-admin; it is not accessible to workspace UID
10001. The host's Ed25519 key is copied directly from /etc/ssh, never fetched
from the network. No SSH password, token, or private key is pasted into chat.

The worker connects to the host's Docker gateway address using strict
host-key checking. SSH port 22 must be accessible from the Docker bridge,
and `openssh-server` must be running on the host.

## Tools

- `vm_host_admin_status` checks configuration and connectivity.
- `vm_host_admin_run(command, timeout_seconds)` executes a host command
  under `sudo -n /bin/bash -lc`. Timeout max 600 s; results are bounded.
- Commands pass the same authenticated owner MCP gateway and durable
  outbound worker queue as workspace operations. Once claimed, they are
  **never automatically retried**; inspect `vm_job_status` if pending.
- An abbreviated SHA-256 of each command and its exit status are logged
  without logging the command itself.

This grants substantial power. The queue must remain owner-only and the
VM_WORKER_TOKEN must remain private. A compromised gateway owner account
could administer the host.

## Verification

After redeploying, `vm_host_admin_status` should report
`configured=true` and `connected=true`. Check:

```bash
cat /sys/fs/cgroup/memory.max
cat /sys/fs/cgroup/cpu.max
cat /sys/fs/cgroup/pids.max
```

Inside DevMCP's worker, the first two should return `max` and the third
should return `2048` unless explicitly overridden. If host SSH fails,
inspect the Docker bridge firewall and sshd service without relaxing
`StrictHostKeyChecking`.

## Revoke access

From the Vultr console:

```bash
sudo rm -f /home/devmcp-admin/.ssh/authorized_keys
sudo rm -f /etc/sudoers.d/devmcp-admin
sudo docker compose -f /opt/dev-mcp/deploy/vm-worker.compose.yml up -d --force-recreate --no-deps devmcp-vm-worker
```

Revocation removes the SSH key authorization and sudo grant; development
workspaces and their data are unaffected. Do not delete the workspace volume
or existing unrelated services.
