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
  the inner write→test→fix recursion. Auth via a **Platform API key**.
- **Reasoning + verifier** — single structured calls through the **LiteLLM
  gateway** (spec / router / memory + the verifier). The verifier runs on a
  **non-Claude** model (Gemini 2.5 Pro) for independent judgment.

**Two recursion bounds**: `MAX_INNER_TURNS` (SDK agent-loop turns per coding
attempt) and `MAX_OUTER_REVISIONS` (verifier revise edges before escalate), plus
`MAX_TOKENS_PER_TASK`.

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

```bash
# Run one task through the graph (a CLI-sourced request):
python -m stromboli run --task "Add a --verbose flag to the CLI"

# Drain every Ready task from the Notion task database (the front-end):
python -m stromboli poll
```

`run --source notion --task-id <page-id>` runs a single Notion task: Stromboli
clones the task's repo into a per-task worktree, drives the graph, opens a PR for
human review (no auto-merge), writes a feedback summary + `Review` status back to
the task, and pings Telegram.

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

Every node emits a Langfuse span under one per-task trace (correlated by
`task_id`); the coding node nests the Agent SDK turns as child spans. Three
per-node eval datasets live in `evals/datasets/` — `spec_eval`, `coding_eval`,
and `verifier_eval` (the most important) — scored against thresholds and gated in
CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
