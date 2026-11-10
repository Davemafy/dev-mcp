# DevMCP: E2B-network fallback for restricted Vultr SSH

The persistent Railway gateway SSH tools remain unchanged. Use this fallback
when Railway TCP/22 times out but an E2B workspace can reach the VM.

1. Call workspace_create() to create a **dedicated** E2B workspace.
2. Independently get the host ED25519 fingerprint from the Vultr console:
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
3. Call workspace_host_prepare(workspace_id, expected_fingerprint). It
   verifies the pinned key directly through the E2B network and generates
   an ephemeral Ed25519 client key **inside E2B**.
4. Run the returned host_setup command *on the trusted Vultr console*.
   This uses Refit's existing authorized_keys helper to grant only restricted
   status / pinned Git-SHA deploy actions for five hours.
5. Call workspace_host_status(workspace_id) and verify authorized: true.
6. workspace_host_deploy(workspace_id, revision) accepts only a full lowercase
   40-character Refit commit SHA; it is **not** a generic shell or a
   Sloppertizer deploy interface.

The client private key is never returned to ChatGPT or copied to Railway.
However, it lives in a disposable E2B workspace. Keep that workspace dedicated
to host access: arbitrary code in the same sandbox can read the private key.
When the workspace expires (typically 30 minutes, maximum 60 minutes here),
its key is lost. Recreate and repeat Vultr authorization for a new workspace.

This is a temporary development-access fallback. A persistent Sloppertizer
deployment should use a separate, non-root, least-privileged identity rather
than reuse Refit's root forced-command account. Avoid modifying Refit services.
