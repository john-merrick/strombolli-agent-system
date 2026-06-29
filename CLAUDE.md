# Stromboli — build conventions & guardrails

Stromboli is an **explicit graph state machine** for agentic coding triage (PRD
v0.2, `docs/prd-stromboli-v0.2.md`). A Notion task is specced → coded (the Claude
Agent SDK agent loop) → verified by an independent **non-Claude** model →
opened as a PR, orchestrated by a LangGraph `StateGraph` with per-node
observability and a three-tier ChromaDB memory.

## The two model surfaces (do not blur them)

- **Coder** — the Claude **Agent SDK** (`claude-agent-sdk`). Its agent loop *is*
  the recursive write→test→fix loop (the inner recursion). Auth via
  `CODER_AUTH_MODE` (§4a): **`subscription`** (default) runs on the logged-in
  `claude` plan tokens with **no** `ANTHROPIC_API_KEY`; **`api_key`** bills per
  token. Code lives in `llm/coder.py`. **Never** reimplement this loop by hand.
- **Reasoning + verifier** — single structured calls through the **LiteLLM
  gateway** (`llm/gateway.py`): spec, router, memory, and the verifier. The
  verifier runs on a **non-Claude** model (Gemini 2.5 Pro) for independent
  judgment — keep it a different family from the coder.

## Architecture map (`src/stromboli/`)

- `state.py` — the single typed `StromboliState` threaded through every node.
- `graph.py` — builds + compiles the `StateGraph` (nodes, conditional edges,
  cycles, checkpointer, `interrupt()`).
- `config.py` — budgets (`MAX_INNER_TURNS`, `MAX_OUTER_REVISIONS`,
  `MAX_TOKENS_PER_TASK`) and the model name per surface.
- `nodes/` — `intake`, `spec`, `router`, `coding`, `verifier`, `pr`, `human`,
  `memory`. One node per file; each is input fields → behavior → output fields.
- `memory/` — ChromaDB three tiers (`procedural`/`semantic`/`episodic`).
  Principle: **retrieve, don't accumulate** — small top-k under a token budget.
- `sandbox/runner.py` — git worktree mgmt + the Docker test runner (the inner
  loop's only oracle).
- `observability/` — Langfuse `tracing.py` (one trace per task, `task_id`
  correlation) and per-node `evals/`.
- `integrations/` — `notion` (intake + write-back), `github` (PR), `telegram`.

## Guardrails

- The coding node works **only** on a throwaway worktree; irreversible actions
  (PR, real-repo writes) happen later, behind the verifier gate.
- The coding node's **only oracle is the sandbox test run** — it never calls the
  verifier or judges its own intent.
- Bound everything: cap SDK turns at `MAX_INNER_TURNS`, revise edges at
  `MAX_OUTER_REVISIONS`, total tokens at `MAX_TOKENS_PER_TASK`. No unbounded
  spins.
- Pre-approve exactly the tools a job needs and **fail closed** on permissions —
  an unattended run must never hang on a prompt.
- Under `subscription` auth, **assert `ANTHROPIC_API_KEY` is absent** — a stray
  key silently flips to pay-as-you-go. A **rate-limit cutoff** is a retryable
  escalation (preserve `session_id`, resume after the window), never a crash.

## Working in this repo

```bash
uv sync --extra dev          # install runtime + dev deps
uv run pytest                # tests
uv run ruff check .          # lint
uv run mypy                  # type check (strict)
python -m stromboli run --task "<text>"   # run one task through the graph
```

Each PRD phase ships green (pytest + ruff + mypy --strict) and is committed
before the next. Tests use injected fakes — no real git, network, Docker, or
live LLM calls in the unit suite.
