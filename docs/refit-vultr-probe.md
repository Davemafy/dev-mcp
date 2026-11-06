# Refit: reproducible live model-migration experiments

**Status as of November 6, 2026:** the original live experiment completed, but
DeepSeek V4.1 Flash and GLM 5.2 each passed its three basic refund cases (3/3).
This was correctly recorded as \`no_reproduced_regression\` — no repair was
claimed.

The stronger experiment in \`scripts/refit_stress_probe.py\` expands this into
eight controlled scenario types. Unit tests use deterministic model doubles;
**a real repair cannot be claimed until a Vultr inference run produces one.**

## Existing engineering capabilities

- Official Vultr OpenAI-compatible chat-completions endpoint.
- Exact response-model identity check; no silent fallback to a different model.
- Real, per-scenario SQLite transaction state and actual tool call execution.
- Independent verification of authorization, refund amount and idempotency.
- Tool-result prompt injection, user attempts to override policies, retries and
  timeout-after-commit behavior.
- Model-generated edits limited to three model-facing string fields.
- Fresh training, validation and protected holdout cases, with comparison
  against the unrepaired candidate on the holdout.
- An explicit request cap to bound the number of billable inference requests.

A *passing* evaluation demonstrates only the specific tested scenarios.
It is not proof of general migration reliability.

## Durable edit → run → inspect loop using GitHub Actions

The connected GitHub integration can edit this repo and inspect GitHub Actions
runs and logs. It cannot read or set GitHub repository secrets.

The workflow \`.github/workflows/refit-live.yml\` runs on GitHub-hosted Linux
and is triggered by a push that changes
\`experiments/refit_runs/trigger.json\` on branch
\`feat/refit-live-inference-probe\`.

- The manifest must specify \`"run_live": true\`.
- The repository must have an Actions secret named
  \`VULTR_INFERENCE_API_KEY\`, configured by its owner.
- The secret is only made available to the two relevant workflow steps.
- Tests execute first and must pass.
- The live step sends a maximum of 100 requests to Vultr inference.
- The job publishes *only a non-sensitive summary*; full synthetic traces stay
  in the runner and are discarded when it terminates.

The source code and the workflow are in a reviewable draft PR, with no changes
to the running FastMCP gateway. This uses ordinary CI, not an SSH or MCP
workaround.

### One-time Actions secret configuration

1. Open GitHub repository \`Davemafy/dev-mcp\`, **Settings**.
2. Choose **Secrets and variables → Actions → New repository secret**.
3. Name: \`VULTR_INFERENCE_API_KEY\`.
4. Paste the key from the user's own Vultr Serverless Inference subscription.
5. Save it. **Never commit the key, paste it in a chat, or expose it in logs.**

The existing VPS secret under \`/etc/refit/inference.env\` remains untouched.
Configuring the GitHub secret is optional if the user prefers to run only on
the VPS, but without either a usable remote execution channel or a GitHub
secret, the assistant cannot independently perform a live inference run.

After that one-time action, updates to the trigger manifest can start further
experiments with no repeated terminal commands.

### Running the richer suite directly on the Vultr VPS

Run from the repository root (Python 3.11+):

    python3 -m scripts.refit_stress_probe \
      --source deepseek-v4.1-flash-1017 \
      --targets glm-5.2,muse-glimmer-30b,raon-1-mini-preview \
      --repair glm-5.3-flash \
      --max-requests 100 \
      --output /opt/refit/refit-stress-report.json

The script reads \`/etc/refit/inference.env\` if the environment variable is
unset. It writes synthetic transaction traces under restrictive file
permissions. Never post a real credential or sensitive production logs.

### Current result vocabulary

- \`source_baseline_insufficient\`: source is not dependable enough for the comparison.
- \`no_reproduced_regression\`: the tested candidate behaves as well as the source.
- \`repair_generation_failed\`: no valid bounded patch was produced.
- \`repair_rejected_training\` / \`repair_rejected_validation\`: the patch didn't pass gates.
- \`repair_failed_holdout\`: apparent improvement didn't generalize.
- \`verified_repair\`: a real repaired candidate improved on unseen cases while passing safety gates.
- \`no_verified_repair\`: no examined candidate achieved the required proof.

The tests never falsify a failure to manufacture a successful repair.

## Deployment and security limitations

This GitHub-hosted experimentation workflow is **not** the final
Vultr-hosted Blast Radius Zero product. It uses Vultr inference, but its
test runner isn't yet hosted on Vultr. The submission must additionally
prove an isolated Vultr runtime with a real resource-exhaustion or other
containment test. Neither the current GitHub workflow nor the fixed SQLite
tools provides arbitrary-code sandbox containment.

Do not merge the experimental workflow into the deployed dev-mcp gateway
until the live results are reviewed. No SSH, deployment, VPS or secret API
backdoor is added to the gateway.
