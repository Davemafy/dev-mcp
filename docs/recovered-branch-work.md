# Recovered experimental work — 2026-11-10

All recovery was performed on `main`. No feature branches are required.

## Recovery method

The historical heads of these deleted branches were attached as commit parents
of [ad8f9d1](https://github.com/Davemafy/dev-mcp/commit/ad8f9d1d0356a0018aa0a6204bce3170d7fc8117)
with an unchanged source tree. This anchors the original commits and their tests
in reachable Git history, rather than only listing SHAs in documentation.

The distinctive source modules and documentation were then copied onto `main`
in [e235a07](https://github.com/Davemafy/dev-mcp/commit/e235a07eb37184e431659701ae052c852023c0fa).

## Original work recovered

| Original branch | Historical tip | Preserved code |
| --- | --- | --- |
| `feat/durable-agent-jobs` | `e4a92a2d153afbde9872a7116b8f8e234953c5de` | `agent_jobs.py`, job documentation, underlying desktop/browser work |
| `feat/agent-progress-guards` | `6f8967977ab652fd447bc3b414dc6364829722c3` | `progress_guard.py`, `observation_delta.py`, updated adaptive browser logic |
| `feat/desktop-adaptive-runtime` | `16b12f42ebd666b04ed6572ebaec55588555661e` | Desktop modules present in durable jobs ancestor |
| `feat/browser-railway-bridge` | `b7cadb41650b952fef1051dafeef57aac4cd4074` | Browser bridge modules present in durable jobs ancestor |
| `feat/refit-live-inference-probe` | `26643f3e0010a2acc401688a013b92c2c44d65da` | `scripts/refit_*_probe.py`, probe documentation |
| `fix/chrome-oauth-callback` | `29623389d99051ce89b64d0e2465db09bdebb726` | Earlier merged fix, original history anchored |
| `feature/self-extending-browser` | `98ea625295bfbc2aba448bf5fa91c02da1110a9f` | Earlier merged browser tool, original history anchored |

The exact original tests and experimental workflow definitions remain accessible
through these commit histories, even where they are not included in the current
working tree.

## Deliberate deployment boundaries

**Recovered does not mean enabled or production-ready.**

The experimental desktop/agent modules are presently inert unless explicitly
integrated with the gateway registry, lifecycle, and HTTP/WebSocket routes.
Avoid enabling them merely by importing code: the original experiments assumed
a stateful Railway process, an E2B sandbox, and (for jobs) a durable SQLite
journal. The newer Vercel gateway uses stateless HTTP and Redis for auth.
An integration must reconcile those differences and pass end-to-end tests.

The Refit probe scripts are independent experiments, not part of DevMCP runtime.

## Remaining uncertainty

Two previously listed branch names, `feature/live-capability-registry` and
`feature/self-extend-browser`, did not have identifiable tips in the recovered
workflow history or PR records. No unique changes from those branches are
claimed recovered. Additional historical SHAs or GitHub event records would
be needed to establish whether they contained unmerged work.

## Next integration gate

Port desktop/browser and jobs into optional, sandbox-only capability modules;
keep every risky feature disabled by default. Run original historical tests
against the integrated gateway, validate real desktop WebSocket transport on
a stateful worker, and preserve the Vercel stateless adapter boundary.
