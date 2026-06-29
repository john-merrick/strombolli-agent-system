# Stromboli — PRD & Build Scope

**Version:** 0.2
**Owner:** Isaac
**Build target:** Claude Code CLI (interactive build) → runtime is a Python service
**Status:** Ready to scaffold

> **v0.2 changes:** Resolved the model topology. Reasoning nodes (spec, router, verifier, memory) run through **LiteLLM**; the coding node runs through the **Claude Agent SDK** (the library form of Claude Code) and its agent loop *is* the recursive write-test-fix loop. The **verifier runs on a non-Claude model** for independent judgment. Coding node authenticates with a **Platform API key** for predictable per-token cost. See §4, §6.4, §6.5, §8.

> **Resolved decisions (this build):** runtime collapses to a **LangGraph runtime + CLI** (no FastAPI dispatch server / queue / ledger), but **Notion API reading is retained** as the front-end for adding tasks (Intake source). Verifier model **pinned to Gemini 2.5 Pro** (§11.1). Reusable I/O (Notion / GitHub / Telegram / worktree / Langfuse) **migrated in place** into the §9 layout. The prior "Ralph loop" and the bespoke `DeterministicPolicy`/`GraphEngine` are removed.

---

## 1. Problem & Premise

The current agentic coding pattern ("Ralph loop") is shallow, repetitious self-correction with no explicit state, no per-stage observability, and no memory across tasks. When it fails, you cannot say *which* part failed or *why*.

Stromboli replaces the loop with an **explicit graph state machine**: named nodes, a single typed state object, conditional routing, and hard verification gates. Control flow is data-dependent and fully traceable. The system ingests a task, produces a spec, writes and tests code, verifies the result against intent, opens a PR, and learns from the outcome.

The defining design choice: **do not reimplement the write-test-fix loop.** The Claude Agent SDK already gives you the same agent loop, tool execution, and context management that power Claude Code. The Coding Node embeds that SDK loop; LangGraph owns everything around it (orchestration, budgets, verification, memory, tracing). The recursion you want lives *inside* the harness — see §6.4.

A second design choice resolved in v0.2: **the system uses two model surfaces.** Reasoning nodes (single structured calls) run through a LiteLLM gateway so the verifier can sit on a *different model family* than the coder — independent judgment, no correlated blind spots. The coding node runs through the Agent SDK on Claude. This is the deliberate split between "embedding an agent loop" (SDK) and "one structured call" (gateway).

---

## 2. Goals & Non-Goals

### Goals
- A LangGraph `StateGraph` with named nodes, conditional edges, cycles, and checkpointing.
- A **two-level recursive loop**: an **inner** loop = the Claude Agent SDK's own agent loop (turn-by-turn write→run→observe→fix, oracle = tests/compiler), bounded by a turn/cost budget; an **outer** loop = the Reflective Verifier's revise edge cycling back to the coding node (oracle = the spec), bounded by a revision cap.
- A verifier that runs on a **non-Claude model** (via LiteLLM) so its judgment is independent of the coder.
- Per-node observability and per-node evals via Langfuse — failures localize to a node.
- A three-tier memory layer (procedural / semantic / episodic) on ChromaDB that closes the loop: verifier reflections persist and are retrieved on future tasks.
- Human-in-the-loop interrupts for ambiguous specs and stuck escalations.
- Real I/O: Notion as system of record, GitHub for PRs, Telegram for notifications.

### Non-Goals (v1)
- No runtime self-rewriting of the graph or prompts. Prompt optimization (DSPy/TextGrad) is **offline** and out of scope for v1.
- No tree/MCTS search. The graph is the only control structure in v1.
- No multi-agent peer/debate topology. Single supervisor-style flow only.
- No autoscaling / multi-worker concurrency beyond what LangGraph offers natively.

---

## 3. Architecture Summary

```
Capture → Spec → Router ──ready──▶ Coding Node (Claude Agent SDK loop, bounded)
                   │                      │   └─ inner recursion: write→test→fix
              unclear│                     ▼
                   ▼                  Reflective Verifier (non-Claude model)
              Human Interrupt              │
                   ▲              ┌────────┼─────────┐
                   └────stuck─────┤      pass    revise──▶ back to Coding Node  ← outer recursion
                                  │        │
                                  │        ▼
                                  │     PR / Commit → Memory Write → Done
                                  └── (escalate to Human)
```

- **Single typed state object** threaded through every node (Pydantic).
- **Two model surfaces:** reasoning nodes via LiteLLM gateway; coding node via Claude Agent SDK (Platform API key).
- **Memory layer** read by Spec/Coding/Verifier, written on completion.
- **Langfuse** wraps every node as a span; the coding node nests the SDK session's turns as child spans (one trace per task across both surfaces).

---

## 4. Tech Stack (resolved)

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Orchestration | LangGraph | StateGraph + checkpointer (SQLite for dev, Postgres later) |
| State schema | Pydantic v2 | Also used for structured LLM outputs |
| **Coding node** | **Claude Agent SDK** (`claude-agent-sdk`, Python) | The SDK's agent loop *is* the recursive write-test-fix loop. Auth via **Platform API key**. Coder model: Claude (Opus 4.8 / Sonnet 4.6). |
| **Reasoning nodes** (spec, router, memory) | **LiteLLM gateway** | Single OpenAI-compatible interface; centralized cost tracking, retries, fallbacks. Route triage/router to a cheap (Haiku-class) model. |
| **Verifier model** | **Gemini 2.5 Pro via LiteLLM** | Deliberately *different* from the coder so blind spots don't correlate. |
| Sandbox | Docker container | Test execution isolated from host; `docker exec` runner. Pre-approve tools, fail-closed on permissions. |
| Memory vectors | ChromaDB | Local, persistent; collections per tier |
| Observability | Langfuse | One trace per task spanning both model surfaces; SDK turns nested under the coding-node span (see §8) |
| Project tests | pytest | The system's own test suite |
| Integrations | Notion API, GitHub API, Telegram Bot API | Notion read = the front-end for adding tasks |

Runs on the Mac Mini M4 (production) with GitHub CI as the external gate.

---

## 5. State Schema

Single object, narrow and semantic. Reducers: append-only fields are `test_results` and `reflections`; everything else overwrites. Implemented in `src/stromboli/state.py` (`StromboliState`, `Spec`, `Verdict`, `TestResult`).

Budgets (config, not hardcoded): `MAX_INNER_TURNS` (Agent SDK agent-loop turns per coding attempt — inner recursion bound), `MAX_OUTER_REVISIONS` (revise-edge cap before escalate — outer recursion bound), `MAX_TOKENS_PER_TASK` (hard cost ceiling across both model surfaces).

---

## 6. Node Specifications

### 6.1 Intake
- Normalizes a Notion task / Telegram message / CLI arg into state. Notion read is the front-end for adding tasks. **DoD:** any valid source produces a well-formed initial state.

### 6.2 Spec
- LLM call via the LiteLLM gateway (structured output → `Spec`). Sets `spec.ambiguous=true` if acceptance criteria can't be pinned down. **DoD:** valid `Spec`; ambiguity flagged on a vague input.

### 6.3 Router (conditional edge)
- Reads `spec.ambiguous`. → Human Interrupt if true, else → Coding Node.

### 6.4 Coding Node — Claude Agent SDK (inner recursive loop)
- The SDK's agent loop is the write→test→fix loop. We only **bound** it (`MAX_INNER_TURNS` + token ceiling), **constrain** it (tool allowlist, fail-closed perms), and **capture** it (diff, last test output, message stream, `session_id`). Only oracle = the sandbox test run. No irreversible actions. **DoD:** known-good spec → passing diff in budget; impossible spec → clean budget exit; non-`success` subtype → node failure.

### 6.5 Reflective Verifier (outer recursive loop)
- Single structured call via LiteLLM on a non-Claude model. Checks the diff against spec intent and whether tests covered the acceptance criteria. On `revise`, the reason is injected into the next Coding Node pass (resuming `session_id`); bounded by `MAX_OUTER_REVISIONS`.

### 6.6 Verdict gate (conditional edge)
- `pass` → PR/Commit. `revise` (under cap) → Coding Node with reason. `escalate`/budget exhausted → Human Interrupt.

### 6.7 PR / Commit
- Branch, commit, open PR via GitHub API. CI is the external gate. **DoD:** dry-run opens no PR but logs intent; live opens a real PR on a test repo.

### 6.8 Human Interrupt
- LangGraph `interrupt()`. Surfaces the question (Telegram + Notion), resumes on edited state.

### 6.9 Memory Write (terminal pre-Done)
- Writes reusable skills → procedural (verified pass only), trace → episodic, conventions → semantic.

---

## 7. Memory Layer

Principle: **retrieve, don't accumulate.** Top-k under a token budget; never carry the whole store.

| Tier | Contents | Store | Written by | Read by |
|---|---|---|---|---|
| Procedural | Verified reusable code/skills; frozen prompts | Chroma `procedural` + repo `skills/` | Memory Write (post-pass only) | Coding Node |
| Semantic | Repo conventions, architecture decisions | Chroma `semantic` (Notion = source of truth) | Memory Write / manual | Spec, Coding |
| Episodic | Task traces + verifier reflections | Chroma `episodic` | Verifier, Memory Write | Spec |

A `revise`/`escalate` reflection is written to episodic; the next similar task retrieves it at Spec. Episodic entries carry timestamps for recency-weighted retrieval/decay.

---

## 8. Observability & Evals

- **Tracing:** every node emits a Langfuse span; coding node nests the Agent SDK message stream as child spans; gateway calls span on the reasoning surface. One correlation id = `task_id`.
- **Per-node eval datasets:** `spec_eval`, `coding_eval`, `verifier_eval` (the most important). CI fails if any node eval regresses below threshold.

---

## 9. Repository Structure

```
stromboli/
  CLAUDE.md
  pyproject.toml
  .env.example
  src/stromboli/
    state.py
    graph.py
    config.py
    llm/{gateway,coder}.py
    nodes/{intake,spec,router,coding,verifier,pr,human}.py
    memory/{store,procedural,semantic,episodic}.py
    sandbox/runner.py
    observability/{tracing.py, evals/{spec_eval,coding_eval,verifier_eval}.py}
    integrations/{notion,github,telegram}.py
  tests/
  evals/datasets/
  docker/sandbox.Dockerfile
```

---

## 10. Phased Build Plan

- **Phase 0 — Scaffold.** deps, `CLAUDE.md`, `state.py`, config, Langfuse + `task_id` correlation, graph of *stub* nodes end-to-end. *DoD:* `python -m stromboli run --task "stub"` completes + one trace.
- **Phase 1 — Deterministic spine.** Real Intake (Notion + CLI), Spec, Router, Verdict gate, PR (dry-run), Human Interrupt; Coding + Verifier stubbed.
- **Phase 2 — Coding Node.** Agent SDK wrapper: bounded loop, tool allowlist + fail-closed, Docker sandbox/worktree, diff + test + `session_id` capture, SDK turns nested in the trace.
- **Phase 3 — Reflective Verifier + outer recursion.** Gemini 2.5 Pro; revise resumes the SDK session; escalate at the cap.
- **Phase 4 — Memory.** Chroma three tiers, retrieve-don't-accumulate, write policy.
- **Phase 5 — Evals.** Three per-node datasets + Langfuse scores + CI gate.
- **Phase 6 — Real I/O.** Notion intake, live GitHub PR, Telegram notify.

---

## 11. Open Decisions

1. **Verifier model — RESOLVED:** Gemini 2.5 Pro.
2. **Coding agent invocation — RESOLVED (v0.2):** Claude Agent SDK (Python library).
3. **Checkpointer backend:** SQLite (dev) assumed; Postgres deferred.
4. **Sandbox repo strategy:** clone-per-task (default).
5. **Procedural memory granularity:** extracted functions with tests (default).
6. **Verifier strictness:** tune against `verifier_eval` after Phase 5.

---

## 12. Success Criteria (v1)

- A task entering via Notion produces a verified PR with no human touch on the happy path.
- Any failure is attributable to a single node via Langfuse + node evals.
- The same class of mistake is not repeated once a reflection exists in episodic memory.
- Token cost per task is tracked and bounded by `MAX_TOKENS_PER_TASK`.
