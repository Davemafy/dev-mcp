# Refit: live Vultr model-migration experiment

This is an engineering test, **not a proven model-migration demo**.

The existing Vultr Serverless Inference API key stays on the user's Vultr VM at \`/etc/refit/inference.env\`. The script never copies it to dev-mcp, Railway, E2B or GitHub.

## What this tests

1. Call the requested source model and target model through actual Vultr inference.
2. Execute their tool calls against independent in-memory SQLite refund scenarios.
3. Verify authorized outcomes from database state, not the model's final text.
4. Require the source to pass three training scenarios; otherwise report \`invalid_source_baseline\`.
5. If the target also passes, report \`no_reproduced_regression\`. **Never fabricate a failure.**
6. If the target fails, call a third Vultr model for a narrowly scoped modification of instructions and tool descriptions.
7. Retest the candidate and evaluate separate validation and unseen holdout scenarios.
8. Report \`verified_repair\` only when all required tests pass.

The program caps its model-call budget (default 70). It does not write to real financial systems, execute model-generated code, or expose an HTTP service.

## Run on the existing Vultr Ubuntu VPS

Download \`scripts/refit_vultr_probe.py\` from the reviewed commit or PR branch, then:

\`\`\`bash
python3 refit_vultr_probe.py \
  --source deepseek-v4.1-flash-1017 \
  --target glm-5.2 \
  --repair glm-5.3-flash \
  --output /opt/refit/refit-live-report.json
\`\`\`

Requires Python 3.11+, outgoing HTTPS, and the pre-existing root-readable key file. No pip dependencies.

The output is a compact status and pass counts. Detailed traces and generated changes are saved with restrictive permissions in the specified JSON report. **Do not paste or publish a report containing genuine customer records**; the current script uses only synthetic orders.

Run the unit tests:

\`\`\`bash
python3 -m unittest tests.test_refit_vultr_probe -v
\`\`\`

The offline tests use explicit deterministic stub responses to test code paths. Those tests **do not demonstrate** genuine autonomous model repair.

## Interpretation

| Status | Meaning |
| --- | --- |
| \`invalid_source_baseline\` | The original model failed training; comparison is invalid |
| \`no_reproduced_regression\` | Both models passed; no repair was needed or attempted |
| \`repair_generation_failed\` | Repair model did not produce a permitted patch |
| \`repair_rejected\` | Proposed patch failed training or validation |
| \`holdout_failed\` | Candidate passed visible cases but failed protected cases |
| \`verified_repair\` | A real target regression was repaired and passed independent scenarios |

All of the above are provisional results until tested on the actual server. The script's fixed tools do not constitute the Blast Radius Zero containment test; Docker sandbox validation is a separate requirement.

This is intentionally a small, auditable addition to dev-mcp. It does not change the deployed FastMCP gateway or attempt to bypass restrictions on remote SSH tool execution.
