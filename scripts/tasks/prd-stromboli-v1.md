# PRD: Stromboli v1 — Agentic Coding-Triage System

## Introduction

Stromboli is an agentic coding-triage system. A human captures a coding task
directly in Notion and writes its spec there, then a self-hosted Python worker
running on a Mac Mini picks it up, builds it with Claude Code (driven by the
**Ralph loop** pattern in an isolated git worktree), runs CI, opens a pull
request, and writes the results back to Notion. The human supervises entirely
from Notion.

The problem it solves: shipping code autonomously is bottlenecked by human
attention. Stromboli maximises the amount of code shipped per unit of human
attention while gating anything risky or unverifiable to human review.

**Notion is the system of record and the sole capture surface.** Hub page
**Stromboli** (`c8d36e2b-ddbd-8346-bd9c-819ae25130c7`) holds two databases:
- `projects` — DB `de8111f8f84c43bdb8dc097f7af45b72`, data source
  `1c928b7d-7206-4ebc-9c08-931ba428cf9b`. Fields: Name (title), Repo
  (text `owner/repo`), Status (select: Active/Done), Tasks (relation → tasks).
- `tasks` — DB `cd7679a9d5db4c4d8bcc147f9e39f7d8`, data source
  `693b2371-5352-4013-90e7-bd9678585a63`. Fields: Task name (title), Project
  (relation), Status (select: To do/Working on/Review/Complete), Spec (text),
  Assigned to (select: Agent/Human), Ready (checkbox), Needs review (checkbox),
  PR (url), Cost (number $), Tokens (number), Created/Last edited (auto).

The human authors the Spec (definition of done) in the task and ticks `Ready`
when it is solid. A Notion automation fires a webhook on `Ready` becoming
checked, which dispatches the worker.

## Goals

- One Notion task (spec'd, `Ready` ticked) → worker opens a correct PR → human
  merges → task marked `Complete`, fully unattended in between.
- Honour the autonomy contract: `Assigned to`, `Ready`, and `Needs review` gate
  what the agent is allowed to do.
- Single serial worker on one repo for v1 (Notion has no atomic lock).
- Every run is observable in Langfuse and idempotent (a re-fired webhook never
  double-claims a task).
- Standard, reusable GitHub Actions verification (tests, lint, types) runs on
  every PR the worker opens.

## User Stories

### US-001: Python project scaffold and configuration
**Description:** As a developer, I need a Python project with centralised
configuration so every component can load secrets and settings consistently.

**Acceptance Criteria:**
- [ ] Python project initialised with `uv` (pyproject.toml, locked deps)
- [ ] A `Settings` object loads from environment/.env: `NOTION_TOKEN`,
      `GITHUB_TOKEN`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
      `LANGFUSE_HOST`, `TUNNEL_PUBLIC_URL`, `WORKSPACE_ROOT`,
      `ANTHROPIC_API_KEY`, `DISPATCH_SHARED_SECRET`
- [ ] Missing required env vars fail fast at startup with a clear error naming
      the missing key
- [ ] `.env.example` documents every variable; `.env` is gitignored
- [ ] Typecheck (mypy/pyright) and lint (ruff) pass

### US-002: Notion task client
**Description:** As the system, I need a typed wrapper over the Notion API so
components read and write task/project fields without duplicating field IDs.

**Acceptance Criteria:**
- [ ] `get_task(page_id)` returns a typed object exposing Task name, Project,
      Status, Spec, Assigned to, Ready, Needs review, PR, Cost, Tokens
- [ ] `get_project_repo(project_relation)` resolves to the `owner/repo` string
- [ ] `update_task(page_id, **fields)` writes Status, PR, Cost, Tokens
- [ ] `append_task_body(page_id, markdown)` appends agent feedback to the page body
- [ ] Status select values map to the exact existing labels
      (To do / Working on / Review / Complete) — no new labels created via API
- [ ] Unit tests cover field read/parse and the repo-resolution path with a
      mocked Notion response (assert on parsed values, not just "no error")
- [ ] Typecheck and lint pass

### US-003: Webhook server and Cloudflare Tunnel
**Description:** As the system, I need a public HTTPS endpoint so Notion can
trigger the worker when a task becomes `Ready`.

**Acceptance Criteria:**
- [ ] FastAPI app exposes `POST /stromboli/dispatch` accepting a JSON body that
      contains the task page ID
- [ ] Endpoint returns `202 Accepted` immediately and processes asynchronously
      (does not block the HTTP response on the build)
- [ ] A shared-secret header (or signature) is required; requests without it get
      `401`
- [ ] `GET /healthz` returns `200`
- [ ] Cloudflare Tunnel config documented in README, mapping the public URL to
      the local port
- [ ] The Notion automation that POSTs to `/stromboli/dispatch` on `Ready`
      checked is documented in README (trigger contract)
- [ ] Unit test: missing/invalid secret → 401; valid request → 202
- [ ] Typecheck and lint pass

### US-004: Dispatch guard and idempotent claim
**Description:** As the system, I need to guard and atomically claim a task so a
re-fired webhook never starts two builds.

**Acceptance Criteria:**
- [ ] On dispatch, fetch the page and proceed only if
      `Ready == true AND Assigned to == Agent AND Status == To do`
- [ ] If the guard fails, log the reason and exit without changes
- [ ] On a valid claim, set `Status = Working on` before any build work begins
- [ ] If `Status` is already `Working on`/`Review`/`Complete`, the dispatch is a
      no-op (idempotent)
- [ ] A single in-process lock ensures only one task builds at a time (serial
      worker); concurrent dispatches queue or are rejected with a logged reason
- [ ] Unit tests cover: guard pass, each guard-fail branch, and double-dispatch
      idempotency
- [ ] Typecheck and lint pass

### US-005: Prepare an isolated git worktree
**Description:** As the worker, I need an isolated git worktree per task so
builds never interfere with each other or the main checkout.

**Acceptance Criteria:**
- [ ] Resolve `owner/repo` from the task's `Project.Repo`
- [ ] Ensure a local clone exists under `WORKSPACE_ROOT`; fetch latest `main`
- [ ] Create a fresh worktree on a new branch named deterministically from the
      task (e.g. `stromboli/<task-id>-<slug>`)
- [ ] Worktree is removed/cleaned up on completion or failure
- [ ] Unit/integration test verifies branch name derivation and cleanup on error
- [ ] Typecheck and lint pass

### US-006: Generate a Ralph prd.json from the Spec
**Description:** As the worker, I need to convert the task `Spec` into a Ralph
`prd.json` so the Ralph loop has discrete, verifiable items to drive.

**Acceptance Criteria:**
- [ ] The task `Spec` is converted to the `{meta, items}` envelope used by this
      repo's Ralph loop (see CLAUDE.md), with `meta.maxAttempts` set
- [ ] Each acceptance criterion maps to an item with `passes:false`,
      `attempts:0`, `blocked:false`
- [ ] `meta.branchName` matches the worktree branch from US-005
- [ ] The generated `prd.json` is written into the worktree
- [ ] Unit test asserts N acceptance criteria produce N well-formed items
- [ ] Typecheck and lint pass

### US-007: Run the Ralph loop in the worktree
**Description:** As the worker, I need to run Claude Code via the Ralph loop so
the task is implemented autonomously against its `prd.json`.

**Acceptance Criteria:**
- [ ] Worker invokes `claude -p` iterations inside the worktree with the repo's
      CLAUDE.md instructions until all items `passes:true` or are `blocked`, or a
      ceiling is hit
- [ ] Per-iteration JSON output is captured (for cost/token/observability)
- [ ] The loop stops on the Ralph completion signal (`<promise>COMPLETE</promise>`)
      or when no eligible item remains
- [ ] `progress.txt` and `prd.json` from the run are retained as run artifacts
- [ ] Integration test (mocked/stubbed CC invocation) verifies the loop
      terminates on the completion signal and on the ceiling
- [ ] Typecheck and lint pass

### US-008: Circuit breaker (cost / loop ceiling)
**Description:** As an operator, I need a circuit breaker so a runaway task can't
burn unlimited cost or loop forever.

**Acceptance Criteria:**
- [ ] Configurable ceilings: max iterations and max cost (USD) per task
- [ ] When a ceiling trips, the loop stops, `Status` is set to `Review`, and a
      note explaining the trip is appended to the task body
- [ ] A trip is logged and traced (Langfuse) with the reason
- [ ] Unit test: simulated cost over ceiling trips the breaker and routes to
      `Review` (assert on the resulting status and note)
- [ ] Typecheck and lint pass

### US-009: Standard GitHub Actions verification workflow
**Description:** As the system, I need a reusable CI workflow so every PR the
worker opens is gated on tests, lint, and types.

**Acceptance Criteria:**
- [ ] A reusable GitHub Actions workflow runs on `pull_request`: install deps,
      run tests, run lint, run type checks
- [ ] The workflow is added to (or referenced by) the target repo so it runs on
      the worker's branch
- [ ] Workflow fails the check when any of tests/lint/types fail
- [ ] README documents how a new target repo opts into the workflow
- [ ] CodeQL and the spec eval suite are explicitly deferred (see Non-Goals)

### US-010: Open the PR and write it back to Notion
**Description:** As the system, I need the worker to open a PR and record its URL
so the human can review from Notion.

**Acceptance Criteria:**
- [ ] After the loop succeeds, commit the worktree changes and push the branch
- [ ] Open a PR against `main` with a title/body derived from the task and spec
- [ ] Write the PR URL to the task's `PR` field
- [ ] If no changes were produced, route to `Review` with an explanatory note
      instead of opening an empty PR
- [ ] Unit test verifies PR-body/title derivation and the empty-diff branch
- [ ] Typecheck and lint pass

### US-011: Integration routing (Review vs Complete)
**Description:** As the system, I need to route the finished task per the
autonomy rules so risky work waits for a human.

**Acceptance Criteria:**
- [ ] If `Needs review == true`, set `Status = Review` (await human approve/merge)
- [ ] If `Needs review == false`, set `Status = Complete` after the PR is opened
      (v1 = human performs the merge; no auto-merge)
- [ ] `Assigned to == Human` tasks are never picked up or modified by the worker
- [ ] Unit test covers all three routing branches against the autonomy table
- [ ] Typecheck and lint pass

### US-012: Write-back of status and feedback
**Description:** As a supervisor, I want the worker's outcome written back to the
task so I can triage from Notion without reading logs.

**Acceptance Criteria:**
- [ ] On completion (success, blocked, or breaker trip), append a feedback
      summary to the task body: what was done, what's blocked and why, and a link
      to the PR
- [ ] `Status` reflects the final routing (Review/Complete) from US-011
- [ ] Write-back is resilient: a Notion write failure is retried and logged, and
      does not crash the worker
- [ ] Unit test asserts the feedback summary includes blocked-item reasons when
      present
- [ ] Typecheck and lint pass

### US-013: Langfuse observability
**Description:** As an operator, I want each run traced in Langfuse so I can see
cost, tokens, and where a run went wrong.

**Acceptance Criteria:**
- [ ] Each task build is a Langfuse trace; Ralph iterations are spans
- [ ] Cost and token usage parsed from CC output are recorded on the trace
- [ ] Circuit-breaker trips and failures are tagged on the trace
- [ ] Writing `Cost`/`Tokens` back to Notion is explicitly deferred (Non-Goals)
- [ ] Typecheck and lint pass

## Functional Requirements

- FR-1: The system MUST resolve the target repo for a task from its
  `Project.Repo` (`owner/repo`).
- FR-2: The webhook endpoint `POST /stromboli/dispatch` MUST accept a task page
  ID, require a shared secret, return `202` immediately, and process the build
  asynchronously.
- FR-3: The worker MUST only act on a task when
  `Ready == true AND Assigned to == Agent AND Status == To do`, and MUST set
  `Status = Working on` as an idempotent claim before building.
- FR-4: Dispatch MUST be idempotent — a duplicate webhook for an already-claimed
  task is a no-op.
- FR-5: Only one task MUST build at a time in v1 (serial worker).
- FR-6: Each build MUST run in an isolated git worktree on a dedicated branch,
  cleaned up afterward.
- FR-7: The worker MUST convert the task `Spec` into a Ralph `prd.json`
  (`{meta, items}`) and drive `claude -p` iterations per this repo's CLAUDE.md
  until completion, block, or a ceiling.
- FR-8: A circuit breaker MUST stop the run on a configurable cost or iteration
  ceiling, set `Status = Review`, and note the trip on the task.
- FR-9: A reusable GitHub Actions workflow MUST run tests, lint, and types on
  every PR the worker opens.
- FR-10: The worker MUST open a PR and write its URL to the task `PR` field; an
  empty diff MUST route to `Review` instead of opening an empty PR.
- FR-11: Routing MUST follow the autonomy table: `Needs review == true` →
  `Review`; otherwise → `Complete` (human merges in v1).
- FR-12: The worker MUST append a feedback summary (work done, blockers, PR link)
  to the task body and set the final `Status`.
- FR-13: Irreversible work (deploy, secrets, migrations, anything client-facing)
  MUST be treated as `Needs review` and never auto-completed.
- FR-14: Each run MUST be traced in Langfuse with cost/token usage and
  failure/trip tags.
- FR-15: Select/status labels MUST use the exact existing Notion labels; the
  system MUST NOT attempt to create new select options via the API.

## Non-Goals (Out of Scope for v1)

- Telegram (or any chat-based) capture and any conversational intake agent —
  tasks are captured and spec'd directly in Notion by the human.
- Auto-merge of any kind (tier A / green-CI auto-merge) — phase 2.
- Parallel worker fleet and Notion claim-lock fields — phase 2 (serial only now).
- GitHub coding-agent execution backend (Ralph loop is the v1 backend).
- Notion form-based capture (manual Notion entry only in v1).
- Writing `Cost`/`Tokens` back to Notion (traced in Langfuse only for v1).
- CodeQL scanning and the deeper spec eval suite in CI.
- External relations (GitHub PR objects, Sprint) — phase 2.
- Multi-user / team workflow.
- Any general project-management features — coding tasks only.

## Technical Considerations

- **Runtime:** Python, dependency-managed with `uv`; FastAPI for the webhook;
  Notion, GitHub, and Langfuse SDKs.
- **Execution backend:** Ralph loop pattern from this repo's `CLAUDE.md` —
  per-task generated `prd.json`, iterative `claude -p`, self-blocking at
  `meta.maxAttempts`, completion via `<promise>COMPLETE</promise>`.
- **Capture:** manual Notion entry; the human writes the Spec and ticks `Ready`.
  A Notion automation on the `tasks` DB POSTs to the webhook on `Ready` checked.
- **Isolation:** one git worktree + branch per task under `WORKSPACE_ROOT`.
- **Idempotency/locking:** Notion has no atomic lock, so v1 relies on the
  guard + `Working on` claim + a single in-process serial lock.
- **Networking:** Cloudflare Tunnel exposes the Mac Mini's local FastAPI port.
- **Secrets:** all tokens via environment/.env; never committed.

## Success Metrics

- % of tasks completed with no human edit to the PR.
- Human touch-time per completed task.
- Cost per completed task (from Langfuse).
- Escape rate (merged PRs later reverted).

## Open Questions

- What concrete cost/iteration ceilings should the circuit breaker default to?
- Should the worker run as a long-lived service (e.g. managed by pm2/launchd),
  and how is it restarted on crash?
- For US-009, is the standard workflow committed into each target repo, or kept
  as a reusable workflow referenced from a central repo?
- What is the retry policy for transient Notion/GitHub API failures during
  write-back?
