# Dev MCP — full remote desktop and adaptive browser

This experimental implementation extends the existing E2B desktop bridge on
`feat/desktop-adaptive-runtime`. It is not deployed to the production MCP
gateway and must not be advertised as an operational ChatGPT sidebar until the
gateway and plugin UI integration pass live acceptance.

## Architecture

- **Gateway (Railway):** single-owner OAuth/MCP, plus signed HTTPS and
  WebSocket proxy for noVNC. It injects the E2B traffic access token on the
  server side; it never makes the token visible in noVNC HTML or JavaScript.
- **Sandbox (E2B):** virtual X server on `:99`, Fluxbox, x11vnc bound to
  `127.0.0.1:5901`, websockify/noVNC on `3488`. The gateway proxies
  only this fixed port with a 20-minute signed `desktop-view` ticket.
- **Visible Chromium:** launched on `DISPLAY=:99`, with CDP bound to
  `127.0.0.1:9222`. It has a dedicated E2B browser profile; do not copy
  personal credentials into that profile.
- **MCP controls:** `desktop_setup`, `desktop_start`, `desktop_status`,
  `desktop_browser_open`, `desktop_stop`, `browser_observe`,
  `browser_adaptive_run`; existing `browser_stream_action` and
  `browser_capture` remain available.
- **Code execution:** the owner-authenticated `write_file` and
  `run_terminal_command` tools already run arbitrary code *inside E2B*.
  Agent-written Python/Playwright scripts can attach via
  `p.chromium.connect_over_cdp("http://127.0.0.1:9222")` to control
  the same browser that noVNC displays. The gateway does not evaluate
  arbitrary scripts and the debugging port is not a public desktop endpoint.

## Setup

1. Deploy this branch as a staging Dev MCP gateway, maintaining the
   existing owner OAuth configuration. **Do not change production deployment
   without authorization.**
2. Create a signed E2B workspace using `workspace_create`.
3. Call `desktop_setup` to install Xvfb, x11vnc, Fluxbox, noVNC,
   websockify and scrot. Call `browser_setup` to install Playwright and
   Chromium, which also enables swap in small E2B workspaces.
4. Call `desktop_start`, then `desktop_browser_open` with a trusted test
   URL. Open the returned **private** `viewer_url`.
5. Call `browser_observe` to retrieve interactive DOM elements and
   `browser_adaptive_run` with a model-generated action sequence.
6. Call `desktop_stop` and `workspace_close` after acceptance.

### Self-correction demonstration

On a controlled test fixture, run:
1. `browser_adaptive_run` with `[{"action":"click","selector":"#submit"}]`.
   The click fails, and the tool returns current DOM elements.
2. Locate the replacement `data-testid` in the observation.
3. Reinvoke `browser_adaptive_run` with the updated selector, followed by
   `assert_text`. The tool validates the visible outcome.

The tool does not contain an independent LLM; ChatGPT or another model must
perform the interpret–revise–retry step. Scripts using the CDP endpoint can
implement more complex workflows, but they have the same trust level as
unrestricted E2B terminal commands.

## Safety boundaries

- The user must authorize the websites/accounts being automated.
- Do not use the system to bypass bot barriers, manipulate third-party votes,
  or perform transactions without explicit user confirmation.
- A short-lived desktop viewing URL is a **bearer capability**. Whoever
  possesses it can control the desktop until it expires; do not paste it into
  public issues, logs, prompts or demo recordings.
- x11vnc has no separate password because the service is loopback-only and
  controlled by two upstream layers: E2B's traffic access token and the
  gateway's signed viewer ticket. This single-owner arrangement is not
  sufficient for untrusted multi-user tenancy; add per-viewer authorization,
  revocation, quotas and audit records before exposing it to other users.
- Do not expose port `9222` or the X11 socket publicly. Agent-written
  Playwright code can read the sandbox browser's authenticated session.
- Limit workspace lifetime, concurrent viewers, action rate and E2B costs.
- A real desktop is provided by VNC. Actual frame rate depends on machine,
  viewport, network and encoding; **no fixed frame rate has been measured**.
- ChatGPT MCP Apps UI is a separate presentation integration. The external
  viewer works as a standalone webpage once its Railway gateway route is
  deployed. Embedded iframe use requires CSP/permission verification and
  may be subject to additional ChatGPT plugin review.

## Verification receipts (November 7, 2026)

- Installed Xvfb/Fluxbox/x11vnc/websockify/noVNC in E2B.
- Observed actual VNC WebSocket greeting `RFB 003.008`.
- A browser opened the noVNC page, rendered its canvas, and reported
  `Connected to e2b.local:99`.
- Headful Chromium ran visibly on the X11 desktop; a real framebuffer
  screenshot was inspected.
- On a locally owned fixture, the first selector failed with `TimeoutError`,
  the DOM returned the new `data-testid`, and a revised action sequence
  reached `Verified: adaptive action succeeded`.
- A separate model-written Playwright script connected over CDP to the
  existing headful browser and repeated the repair successfully.
- The Railway HTTP/WS gateway has mocked relay tests. It **has not yet**
  been deployed and accepted through a real external viewer session.
- Local full test suite: run `python -m pytest -q`; the final count must
  be reported from a fresh run at the committed SHA.
