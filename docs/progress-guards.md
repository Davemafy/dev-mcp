# Browser progress and context guards

Experiment branch: `feat/agent-progress-guards`.

These features reduce repetitive MCP browser work and model-visible tool
output. They do not circumvent ChatGPT quotas, usage limits, or client-side
message policies.

## Three independent boundaries

1. **Live E2B no-progress circuit** (`progress_guard.py`):
   for each mutating browser action, compute a SHA-256 digest of locally
   observed URL, title, visible text, scroll offset and actionable DOM.
   If an identical mutation at the same fingerprint makes no observable
   progress three times, reject the fourth *before* calling Playwright.
   Different selectors remain available for model-led repair, while a
   successful page change clears the old no-progress counters. No passwords
   or raw DOM observations are stored in the guard; it retains bounded
   digests only. It is scoped to the live browser process.

2. **Bounded model repair sequence** (`adaptive_browser.py`):
   `browser_adaptive_run` returns a `repair_id` on failure. The model must
   pass it on corrective calls. The limit is the original attempt plus
   `max_repairs` corrections (default 2), after which that repair session
   stops. Increasing the value on a later call cannot increase the
   original budget. A fresh `repair_id` begins a separate objective,
   so the E2B no-progress circuit remains the independent defense against
   repeated identical mutations. Repair tickets are an in-memory,
   one-hour, at-most-128-session cache in the gateway; they do not survive
   a Railway process restart.

3. **Deterministic semantic delta** (`observation_delta.py`):
   `browser_observe_delta(workspace_id)` initially returns a complete,
   bounded semantic snapshot and `snapshot_id`. Calling it again with
   `base_id=snapshot_id` returns changed elements and text fields only.
   An unknown/stale ID returns a full snapshot instead of silently dropping
   information. The cache is per-workspace, bounded to 64 entries and
   expires after 30 minutes. The original `browser_observe` and
   `browser_capture` still provide full structure and pixels respectively.

## Limitations

- Hashing visual state from text and DOM attributes does not detect all
  progress; e.g. network side effects, silent downloads or CSS-only changes
  may not change the fingerprint. The circuit stops repeating an action,
  not all possible looping strategies.
- Browser view frames are already separate from model context. Compacting
  `browser_observe` results doesn't compress VNC video or change ChatGPT
  usage allowances.
- A repeated action with no visible effect could still be intentional.
  Change page state or start a fresh desktop/browser session if legitimately
  repeating a no-op. There is no model-accessible `guard_reset` escape.
- Different browser tasks need explicit objective IDs or an outer durable
  job runner for stronger cross-request budgets. The E2B circuit state is
  reset when the browser itself is restarted.
- The semantic delta reports changed observed fields; it is not lossless
  for the entire DOM, CSS, screenshots, or state outside the observation
  contract. Use an image whenever layout or visual quality matters.

## Verification

- Local unit suite: `python -m pytest -q`; the committed SHA must pass
  before merge or deployment.
- A real Chromium fixture confirmed three unchanged clicks were observed,
  the fourth was rejected, and a genuine title mutation reset the guard.
- Regression tests cover retry-budget exhaustion across calls, stale
  snapshot fallback, per-workspace isolation and output reduction on a
  repeated semantic snapshot.

No Railway deployment or ChatGPT-host verification is implied by these tests.
