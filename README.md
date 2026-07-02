# Stromboli

Stromboli is an **explicit graph state machine** for agentic coding triage. A
task captured in Notion is turned into a spec, coded by the **Claude Agent SDK**
agent loop, verified by an **independent non-Claude model**, and opened as a pull
request — orchestrated by a LangGraph `StateGraph` with per-node observability
and a three-tier memory that learns across tasks.

It replaces the old "Ralph loop" (shallow self-correction with no explicit state,
observability, or memory) with named nodes, a single typed state object,
conditional routing, and hard verification gates. See the full spec in
[`docs/prd-stromboli-v0.2.md`](docs/prd-stromboli-v0.2.md).

## Architecture

```
Capture → Spec → Router ──ready──▶ Coding Node (Claude Agent SDK loop, bounded)
                   │                      │   └─ inner recursion: write→test→fix
              unclear│                     ▼
                   ▼                  Reflective Verifier (Gemini 2.5 Pro)
              Human Interrupt              │
                   ▲              ┌────────┼─────────┐
                   └────stuck─────┤      pass    revise──▶ back to Coding Node  ← outer recursion
                                  │        │
                                  │        ▼
                                  │     PR / Commit → Memory Write → Done
                                  └── (escalate to Human)
```

**Two model surfaces** (kept deliberately distinct):

- **Coder** — the Claude **Agent SDK** (`claude-agent-sdk`); its agent loop *is*
  the inner write→test→fix recursion. Auth via `CODER_AUTH_MODE`:
  **`subscription`** (default) runs on the logged-in `claude` plan with **no**
  `ANTHROPIC_API_KEY` in the environment (asserted at startup — a stray key
  silently flips billing); **`api_key`** bills per token.
- **Reasoning + verifier** — single structured calls through the **LiteLLM
  gateway** (spec / prompt / memory + the verifier). The verifier runs on a
  **non-Claude** model (Gemini 2.5 Pro) for independent judgment.

**Three budgets, all enforced**: `MAX_INNER_TURNS` (SDK agent-loop turns per
coding attempt), `MAX_OUTER_REVISIONS` (verifier revise edges before escalate),
and `MAX_TOKENS_PER_TASK` (cumulative tokens across both surfaces, accumulated
on the state as `tokens_used`; the verdict gate refuses further revise cycles
once exceeded).

## Layout (`src/stromboli/`)

| Path | Role |
|------|------|
| `state.py` | the single typed `StromboliState` threaded through every node |
| `graph.py` | builds/compiles the `StateGraph`; `run_task` drives one task |
| `config.py` | budgets + the model name per surface |
| `nodes/` | `intake`, `spec`, `router`, `coding`, `verifier`, `pr`, `human`, `memory` |
| `llm/` | `gateway.py` (LiteLLM structured calls), `coder.py` (Agent SDK wrapper) |
| `memory/` | ChromaDB three tiers (`procedural`/`semantic`/`episodic`) |
| `sandbox/runner.py` | git worktree mgmt + the Docker test runner |
| `observability/` | Langfuse `tracing.py` + per-node `evals/` |
| `integrations/` | `notion` (intake + write-back), `github` (PR), `telegram` |

## Development

Managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev          # install runtime + dev deps
uv run pytest                # tests (offline — injected fakes, no live calls)
uv run ruff check .          # lint
uv run mypy                  # type check (strict)
```

## Running

Secrets in `.env` are 1Password references, so run through `op run` (a bare
invocation passes the literal `op://…` strings and every gateway call 401s):

```bash
# Run one task through the graph against a target repo (CLI-sourced).
# --repo is a local path or a GitHub owner/name; --dry-run-pr logs the PR
# intent instead of pushing.
op run --env-file=.env -- uv run python -m stromboli run \
  --task "Add subtract() to calc.py with tests" \
  --repo /path/to/target-repo --dry-run-pr

# Drain every Ready task from the Notion task database (the front-end):
op run --env-file=.env -- uv run python -m stromboli poll

# Watch Notion continuously and build Ready tasks as they appear:
op run --env-file=.env -- uv run python -m stromboli watch
```

`run --source notion --task-id <page-id>` runs a single Notion task: Stromboli
clones the task's repo into a per-task worktree, drives the graph, opens a PR for
human review (no auto-merge), writes a feedback summary + `Review` status back to
the task, and pings Telegram. A CLI-sourced task gets the same clone-per-task
worktree from `--repo` and skips all Notion write-backs.

## Configuration

All configuration is environment-backed (optionally via a local `.env`; see
[`.env.example`](.env.example)). Required variables fail fast on startup. Notion
is the front-end for adding tasks: tick **Ready** on a task assigned to the agent
and `poll` picks it up (guard: `Ready ∧ Assigned to == Agent ∧ Status == To do`).

The Docker sandbox image (test isolation) is built from
[`docker/sandbox.Dockerfile`](docker/sandbox.Dockerfile):

```bash
docker build -f docker/sandbox.Dockerfile -t stromboli-sandbox:latest .
```

## Observability & evals

**Langfuse is the source of truth for "where is this task and why did it do
that."** Every node emits a span under one per-task trace (correlated by
`task_id`) carrying the node's input, its output (the state diff), latency, and
token usage; the coding node nests the Agent SDK turns as child spans and the
gateway nests one `llm-call` span per model call. A local `trace.md`/`trace.jsonl`
is also written per run as the offline fallback. See
[`docs/observability.md`](docs/observability.md) for how to open and read a
trace.

Three per-node eval datasets live in `evals/datasets/` — `spec_eval`,
`coding_eval`, and `verifier_eval` (the most important) — scored against
thresholds and gated in CI
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
