# Persistent DevMCP → Vultr connection (single owner)

DevMCP's normal ChatGPT tool contract stays in Railway; E2B still works.
A small VM-side worker **pulls** authenticated tasks from Railway over HTTPS.
No inbound VM ports, SSH key expiry, new AI model, Docker socket exposed to
workspace code, or shared Refit filesystem.

## One-time Railway setup

1. Confirm the Railway persistent volume is mounted **at /data**.
2. Set `VM_WORKER_TOKEN` to a freshly generated random value of at least
   32 characters. Prefer `openssl rand -hex 32`. Keep it in Railway Secrets;
   **never paste the token in chat or commit it to Git**.
3. Optional: `VM_JOB_DB=/data/devmcp-vm-jobs.sqlite` (this is the default).
4. Keep `WORKSPACE_BACKEND=e2b` so existing E2B handles continue to work.
   Keep `AGENT_JOBS_ENABLED=0`: **ChatGPT is the agent, not a second model**.
5. Redeploy the latest `main`. The public `/health` endpoint then includes
   `vm_link.configured=true`; `worker_connected` will be false until the
   VM service starts.

The SQLite queue journals requests and task receipts on /data. A claimed task
is **never automatically rerun** after losing the worker; check
`vm_job_status(job_id)` before taking action. If the worker is offline when
a command is submitted, that command is rejected rather than silently queued.

## One-time Vultr console setup

On the existing Ubuntu VM, use a root console session. This is the VM hosting
Refit, so **do not** modify Refit's containers, ports, SSH authorized_keys,
deployment scripts or volumes.

Use the same random token configured in Railway. Create a private VM environment
file **on the VM** (do not paste the token into ChatGPT):

```bash
sudo install -d -m 0700 -o root -g root /etc/devmcp
sudo nano /etc/devmcp/vm-worker.env
```

Its contents should look like:

```dotenv
VM_GATEWAY_URL=https://dev-mcp-production-8e50.up.railway.app
VM_WORKER_TOKEN=REPLACE_WITH_THE_SAME_LONG_RANDOM_RAILWAY_SECRET
```

Then:

```bash
sudo chmod 600 /etc/devmcp/vm-worker.env
sudo chown root:root /etc/devmcp/vm-worker.env
git clone https://github.com/Davemafy/dev-mcp.git
cd dev-mcp
sudo bash deploy/bootstrap-vm-worker.sh
```

The script checks the required settings and starts a **separate** Docker
service with `restart: unless-stopped`. Docker already being installed is a
prerequisite. No DNS change or open inbound TCP port is necessary.

To inspect only the dedicated service:

```bash
sudo docker compose -f deploy/vm-worker.compose.yml ps
sudo docker compose -f deploy/vm-worker.compose.yml logs --tail=40
```

To stop only this worker:

```bash
sudo docker compose -f deploy/vm-worker.compose.yml stop
```

## Security and capacity

- Worker polls Railway over verified HTTPS; the API requires a high-entropy
  bearer token on every claim/report request.
- Commands run as a separate unprivileged Linux UID **inside** the dedicated
  Docker container. The worker's token is removed from child environments.
- The worker container's root filesystem is read-only, no Refit directories
  are mounted, no host Docker socket is exposed, Linux capabilities are
  reduced, and there are **no published ports**.
- The owner-operated worker has no Docker-imposed CPU, RAM or PID cap.
  Host resource availability and kernel limits still apply; bounded logs
  remain enabled. It shares the VM's underlying disk. Do **not** run untrusted
  or uncontrolled disk-intensive workloads on a shared production host.
- Files survive worker container restarts under `/srv/devmcp-workspaces`.
  Background *processes* do not survive container recreation; the filesystem
  and execution receipts do.
- No cross-workspace isolation is promised **within** this single-owner
  container; use a separate sandbox per untrusted tenant.

## Validation

After the new VM container is started and Railway is deployed, inspect the
authenticated `vm_status` tool or public `/health` response. It should report:

```json
"vm_link": {"configured": true, "worker_connected": true}
```

Then, from the authenticated ChatGPT MCP connection:

1. Call `vm_workspace_create`; get a signed workspace ID.
2. Call `vm_run(workspace_id, "pwd && node --version && python3 --version")`.
3. Write, read, and list a small test file, then check recovery via
   `vm_workspace_recover`.
4. Restart **only the devmcp worker container** and confirm that the workspace
   and files persist and that `worker_connected` returns true.

These are required live acceptance checks. Unit tests and green GitHub CI
**do not establish that the VM is connected**. If new tool names don't appear
in ChatGPT after deployment, reconnect/refresh the DevMCP MCP connection.

## Limitations

This first worker provides persistent Linux development commands, file APIs and
process lifecycle with a bounded request/receipt queue. Its container does
not yet supply a full desktop or accessible WebGL preview; use the existing
E2B browser capture tools for that workflow until an authenticated viewer
relay is implemented.
