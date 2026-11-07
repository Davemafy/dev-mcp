# Persistent autonomous agent jobs

Experimental implementation on \`feat/durable-agent-jobs\`. The base Dev MCP
gateway and production environment are unchanged until staging deployment.

## What is implemented

- SQLite WAL journal with complete job state, event receipts, actions and
  observations. It must be stored on a real Railway volume at \`/data\`.
- One gateway-owned worker that runs independently of the originating chat
  request once the Railway service is running.
- Model-directed **one action at a time**, using an explicitly configured
  OpenAI-compatible chat completion endpoint (the provider remains your choice).
- Actions use the existing signed E2B workspace, not the gateway host:
  observe DOM, perform approved browser action, execute approved terminal
  command, write a file or request human intervention.
- Separate permission switches for browser interactions and shell/file edits.
  Both default off.
- Maximum 12 executed steps per job; maximum 8 active jobs, bounded model
  requests, short command timeouts and 2 attempts for transient model failures.
- Checkpoint BEFORE each action. If a worker crashes while an action could have
  run, it transitions to \`needs_review\`, not automatic replay. If only the
  model-planning phase was interrupted, the job safely returns to the queue.
- Owner-scoped pause, cancel, resume, status, events and list MCP tools.
- Verified completion requires a successful terminal test or browser assertion.
  A model declaration alone is insufficient.

## Required staging configuration

Configure a **separate Railway staging service** and keep all API keys in
Railway variables, never in chats, repository files, browser scripts, or E2B:

\`\`\`dotenv
AGENT_JOBS_ENABLED=1
AGENT_JOBS_DB=/data/devmcp-jobs.sqlite
AGENT_MODEL_ENDPOINT=https://YOUR-MODEL-PROVIDER/v1/chat/completions
AGENT_MODEL_NAME=YOUR-INFERENCE-MODEL-ID
AGENT_MODEL_API_KEY=SECRET
E2B_API_KEY=SECRET
\`\`\`

Mount a persistent Railway volume at exactly \`/data\`; the controller
refuses to accept jobs without a real mounted volume. A bare directory under
\`/data\` is not a persistence guarantee. The worker is OFF by default.

The gateway must use one running instance for the initial release.
SQLite leases make job claims transactional but multi-replica deployment,
owner revocation, billing caps, and worker scheduling need additional review.
A Railway service restart does not restore expired E2B files.

## Tools exposed

- \`agent_job_submit(workspace_id, goal, max_steps=8, allow_shell=false, allow_interaction=false)\`
- \`agent_job_status(job_id)\`
- \`agent_job_events(job_id, limit=30)\`
- \`agent_job_list(limit=15)\`
- \`agent_job_control(job_id, action="pause|resume|cancel")\`
- \`agent_job_runtime()\`

The job submit operation verifies the signed workspace handle and confirms the
E2B workspace currently exists. The stored job holds only its sandbox ID;
the worker signs a fresh short-lived handle for each action.

## Safety requirements

\`allow_shell=true\` permits generated commands and file changes *inside
the E2B sandbox*. Use it only for a disposable development workspace with
no sensitive credentials or unapproved network reachability. Generated
commands are not sandboxed further than E2B; do not use it for production
accounts or private data. A page or repository may contain prompt injection.
The planner explicitly treats external content as untrusted, but that is not
a complete defense. Scope network egress, repository write rights and secrets
before giving this tool to anyone beyond the owner.

\`allow_interaction=true\` is separately required for clicks, form filling,
keyboard input and navigation. Read-only observation, waiting, text assertions
and scrolling do not require it. Never automate financial transactions,
voting, messages to third parties or access-control bypass without separate
authorizations and policy enforcement.

Pause and cancel take effect between bounded actions. A process cannot
instantly interrupt an active command. A job that exhausted its step budget
cannot resume by resetting the counter.

## Verification evidence

- The branch's complete Python suite passed **77 tests** locally in E2B,
  including database migration, transactional leases, crash recovery,
  authorization boundaries, model transport parsing and agent self-repair.
- A separate E2B worker process was launched before the job was submitted.
  Its task was to fix a real failing Python calculator fixture and run tests.
  The initial two tests failed; the worker inspected the file, edited it, reran
  pytest, and persisted \`completed\` with \`verified=true\` after four actions.
  The status and event history were read from another terminal invocation.
- The integration fixture used a **deterministic stand-in planner**, not live
  inference. This proves the durable execution loop and real file/test actions,
  NOT an independently verified LLM-planned repair.
- A fully connected staging acceptance still requires a configured model key,
  a mounted volume, and a real deployed browser/tool integration test.

## Still missing

This is a functioning durable agent job controller, not a complete Work clone.
There is no multi-hour E2B snapshot restore, independent ChatGPT push
notification, genuine multi-tenant security, or user-facing job console yet.
Do not claim that a disconnected ChatGPT chat is continuing on its own:
the work is executed by the separately deployed Railway worker, and can be
observed through MCP status/events tools.
