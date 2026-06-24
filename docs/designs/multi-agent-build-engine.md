# Design: Multi-Agent Build Engine (replacing the Ralph loop)

**Status:** Approved design — pending implementation
**Date:** 2026-06-24
**Scope:** Replace Stromboli's in-worktree execution engine (the Ralph loop in
`loop.py`) and its static regex planning (`prd.py` compile) with a deterministic
multi-agent state graph. Keep the surrounding pipeline unchanged.

---

## 1. Understanding Summary

- **What:** Replace the Ralph loop + static `prd.json` compile with a state
  graph: **Planner → Worker → Objective Gate → Verifier → Reflector → Integrator**,
  with bounded reflect/retry. Keep worktree isolation, dispatch guard, PR
  mechanics, Notion routing, write-back, observability, and the circuit breaker.
- **Why:** The Ralph worker grades its own homework → false-green PRs. An
  independent, spec-grounded verifier plus reflection fixes **in-task correctness**.
- **Who for:** A solo operator running an autonomous coding-triage worker that
  turns Notion tasks into PRs for human review.
- **Quality mechanism:** Layered verification — a cheap objective gate
  (independently re-run tests/lint/types) per unit, plus a deep spec-grounded
  judgment over the whole diff before the PR opens, by a fresh verifier that can
  reject hollow/tautological tests. Failures bounce back to the Worker with
  targeted feedback.
- **Governing constraint:** Correctness-first, bounded by the existing circuit
  breaker (cost + iteration ceilings). On exhaustion → route to Review with
  accumulated feedback, never a dubious PR.
- **Durability principle (kept from Ralph):** all state on disk + git →
  resumable, crash-safe, auditable. The plan-of-record is Planner-authored, not
  regex-compiled.

## 2. Non-Goals (this design)

Cross-task learning / persistent codebase memory; self-improving harness;
durable dispatch queue; stale-main rebase; auto-merge. All explicitly deferred —
this design targets **in-task correctness only**.

## 3. Assumptions

1. Serial, one-task-at-a-time worker unchanged; concurrency not in scope.
2. Reflect/retry reuses per-unit `maxAttempts` (K=3) + circuit breaker — no new
   unbounded loop.
3. Each agent is a fresh, stateless-per-step `claude -p`-style invocation reading
   /writing disk state (same model as Ralph), not a long-lived process.
4. Worker authors the tests; Verifier critiques them (can reject hollow tests but
   does not author them).
5. Single verifier by default; multi-vote panel is a deferred toggle.
6. Each agent invocation is a Langfuse span under the build trace (extends
   US-013).
7. We evolve the existing repo; the new engine is A/B-able behind a config flag.
8. Security/maintenance surface unchanged (self-hosted, single owner,
   skip-permissions inside the isolated worktree).

## 4. Decision Log

| # | Decision | Alternatives rejected | Why |
|---|---|---|---|
| D1 | Goal = in-task correctness | cross-task / self-improving / pure orchestration | #1 pain is false-green PRs |
| D2 | Layered verifier (objective + spec-grounded, can reject hollow tests) | objective-only; verifier-writes-tests; human-only | Breaks self-judging without over-engineering |
| D3 | Verify per-unit (cheap) + whole-diff (deep) | per-item only; per-task only | Early regressions + late coherence |
| D4 | Replace loop + static planning; keep orthogonal pipeline | evolve Ralph; rebuild whole worker | Clean model, keep tested I/O |
| D5 | Correctness-first, bounded by breaker | hard budget; max-correctness; throughput-first | Spend for correctness, never unbounded |
| D6 | Durable plan-of-record on disk (Planner-authored) | in-memory graph | Keep resumable/auditable property |
| D7 | Pluggable `ControlPolicy` seam | hard-coded control flow | Future supervisor (Approach 2) is a swap, not a rewrite |
| D8 | Reflection bounded by K + breaker | separate reflection cap | Simplest, consistent with current model |
| D9 | Dynamic re-plan in v1 (Reflector→replan→Planner) | plan-once | User wants it; guarded by a re-plan counter |
| D10 | Any blocked unit → whole task to Review | ship verified + flag blocked | Fail-closed; no partial PRs |
| D11 | Same model for all nodes | tiered per-node | Simplest; tune later |

## 5. Final Design

### 5.1 Architecture

A **state graph** executed by a deterministic orchestrator replaces `loop.py`
and the `prd.py` compile. The orchestrator loops:

```
while not terminal and breaker OK:
    action = CONTROL_POLICY(state)     # the swappable seam (D7)
    run node(action) → persist state to disk
```

- **Nodes:** Planner (agent), Worker (agent), Objective Gate (code), Verifier
  (agent), Reflector (agent), Integrator (code).
- **`ControlPolicy(state) → Action`** is the one swappable seam.
  `DeterministicPolicy` now (rules over state); `SupervisorAgentPolicy` later
  (LLM decides). Nodes never know who called them.
- The circuit breaker wraps the whole loop; on ceiling the policy is forced to
  `RouteToReview`.

### 5.2 Components

| Node | Type | Reads | Writes / Returns |
|---|---|---|---|
| Planner | agent | task spec (+ replan feedback) | `plan.json`: ordered units, each w/ acceptance criterion + definition-of-done |
| Worker | agent | `plan.json`, `feedback.md`, target unit | code + tests; bumps `attempts[unit]`. One unit per call |
| Objective Gate | code | the diff | `{pass, captured_output}` from tests/lint/types |
| Verifier | agent | criterion, diff, gate output | `{met, hollow_tests, reasons[]}`. Per-unit (cheap) + whole-diff (deep) |
| Reflector | agent | failing verdict, diff, history | targeted `feedback.md` for Worker, OR a `replan` signal |
| Integrator | code | final state | existing `publish_pr` → `route_task` → write-back → trace |

**`BuildState`** (on-disk, git-tracked, in the worktree): `plan.json`,
`feedback.md`, per-unit `{attempts, last_verdict}`, re-plan counter, agent
transcripts. The policy is a pure function of this state → fully resumable.

**Actions:** `RunPlanner`, `RunWorker(unit)`, `RunVerifier(scope)`,
`Reflect(unit)`, `Replan`, `Integrate`, `RouteToReview(reason)`.

### 5.3 Data flow (happy + reflection + replan)

```
1  RunPlanner            → plan.json [U1,U2,U3]
2  RunWorker(U1)         → code+tests
3  ObjectiveGate         → pass
4  RunVerifier(U1)       → {met:true} → U1 verified
   … U2 …
5  RunWorker(U3)
6  ObjectiveGate         → pass
7  RunVerifier(U3)       → {met:false, reasons:[…]}
8  Reflect(U3)           → feedback.md  (or → Replan if the PLAN was wrong, D9)
9  RunWorker(U3)         → fix, attempts[U3]=2
10 … gate → Verifier(U3) → {met:true}
11 RunVerifier(whole)    → deep pass over full diff vs full spec
      met:true  → Integrate (PR + route, existing)
      met:false → Reflect(scope) → back to Worker
```

Terminal exits besides success:
- **Breaker trips** → forced `RouteToReview`, feedback attached.
- **Unit exhausts K** → that unit blocked; per D10 the whole task routes to
  Review with the blocker surfaced in the write-back summary.

### 5.4 Error handling (fail-closed: ambiguity → Review, never silent pass)

| Failure | Handling |
|---|---|
| Agent invocation fails/timeouts | Retry w/ backoff; after N → `RouteToReview` w/ diagnostic. Never crash worker |
| Malformed agent verdict | Re-prompt once w/ schema; still bad → verdict = **unknown = fail** |
| Gate tooling crash (vs test failure) | Distinguished; tooling crash → retry then Review (don't reflect on a phantom) |
| Crash mid-build | Resume from disk; atomic writes (temp+rename); `attempts` persisted before node runs |
| Reflection / re-plan ping-pong | Bounded by K + breaker + **no-progress detector** (repeated diff hash / unchanged verdict reasons / re-plan counter) → escalate to Review |
| Downstream I/O (push/PR/Notion) | Reuse `resilient_append`, `GitError` handling, empty-diff→Review |

### 5.5 Edge cases

- No real acceptance criteria → empty plan → Review.
- Already satisfied / no-op → empty diff → Review.
- Plan explosion → hard unit cap → Review for re-scoping.
- Tests green but criterion unmet → Verifier overrides gate → Reflect (the core win).
- Verifier false-negative → K-bounded + no-progress → Review (safe failure direction).
- Flaky tests → gate retried; persistent → fail → Review.
- Resume → disk authoritative; node re-run overwrites its slot idempotently.

### 5.6 Testing strategy

- **`DeterministicPolicy` is the heart** → pure `(BuildState) → Action`; exhaustive
  table tests over every branch (gate-fail, verify-reject, all-verified,
  K-exhausted, breaker-tripped, replan, no-progress).
- **Node contract tests** — each agent node's prompt-builder + output parser
  (valid / malformed-fail-closed / schema-reject).
- **Integration tests** — full graph with fake agents returning scripted verdicts
  (à la `RecordingCC`): happy path, reflection, replan, breaker trip, exhaustion,
  resume-from-disk golden fixtures.
- **Forward-compat check** — swap a fake `SupervisorPolicy` to prove nodes/state
  are policy-agnostic (de-risks future Approach 2).
- No real `claude` / network / git in tests.

## 6. Future evolution (out of scope, designed-for)

- **Approach 2 (supervisor agent):** implement `SupervisorAgentPolicy` against the
  same nodes/state. No node or state changes required.
- **Cross-task learning:** lean on target-repo `CLAUDE.md` as durable memory; a
  later iteration could mine `BuildState` transcripts.
- **Per-node model routing, multi-vote verifier panel:** deferred toggles.

## 7. Open risks acknowledged

- Verifier is itself an LLM judgment; grounding it in re-run tests + explicit
  acceptance criteria mitigates but does not eliminate judge error. Mitigation:
  fail-closed + human review of Review-routed tasks.
- Dynamic re-plan (D9) adds an oscillation surface; mitigated by the re-plan
  counter + breaker.
- Cost is materially higher than Ralph per task; bounded by the breaker (D5).
