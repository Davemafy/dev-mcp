# 3D production on the persistent DevMCP VM worker

## What this enables

Use the VM's existing Python/Node toolchain to inspect real binary models,
compare aligned reference and rendered screenshots, and capture browser frames.
The workflow is **code-first**: Blender is optional, not a dependency.

After **both** Railway's DevMCP gateway and the isolated Vultr worker are
redeployed with this revision, you can call `vm_run`:

```sh
python /opt/devmcp/scene_audit.py probe
python /opt/devmcp/scene_audit.py inspect /workspaces/<workspace>/model.glb
python /opt/devmcp/scene_audit.py capture http://127.0.0.1:3000 \
  --output capture.jpg --width 390 --height 844 --require-webgl
python /opt/devmcp/scene_audit.py compare reference.png capture.png \
  --diff difference.png
```

The capture exits with an error if a page raises JavaScript errors, or WebGL2
is unavailable when `--require-webgl` is set. A screenshot is not evidence
of working 3D unless actual graphics capabilities and rendered contents are
checked. Compare only the **same viewpoint and dimensions**.

## Binary artifacts

The authenticated `vm_artifact_read` command returns a **base64 chunk** and
`next_offset` of a file under the signed workspace, up to 24 KB per call.
Allowed types: PNG, JPEG, WebP, GLB, glTF, BLEND, ZIP and MP4. File size
limit: 64 MiB. The gateway does not publish a download URL or host uploads.
Fetch consecutive chunks until `complete`. Larger assets need separately
provisioned private object storage with signed, expiring transfers.

## Capacity

Default Docker Compose budget remains **1 CPU, 1 GiB RAM and 128 PIDs**.
Do not take all the resources of the shared 4 GiB Vultr host: Refit may be
running there. Limits can be overridden only when rebuilding **this worker**:

```sh
DEVMCP_VM_CPUS=1.0 DEVMCP_VM_RAM=1536m DEVMCP_VM_PIDS=160 \
  docker compose -f deploy/vm-worker.compose.yml up -d --build
```

Use at most 1536 MiB initially unless actual host memory peaks and service
headroom have been measured. Changing code on GitHub **does not** apply a
Compose limit on the host or rebuild the worker. No GPU is available.

The worker remains read-only, unprivileged for project commands, with no host
Docker socket, Refit volume, inbound port, or arbitrary host shell.

## Verification

Run `pytest -q tests/test_scene_audit.py tests/test_vm_link.py` locally and
the normal repository CI. After deploying the worker, check `vm_status`,
then `vm_run` with `python /opt/devmcp/scene_audit.py probe` and
`python -m playwright --version`. An end-to-end mobile browser capture
and chunk retrieval must pass **before** declaring the feature live.
