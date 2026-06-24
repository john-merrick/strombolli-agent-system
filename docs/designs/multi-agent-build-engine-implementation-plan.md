# Implementation Plan: Multi-Agent Build Engine

**Companion to:** `multi-agent-build-engine.md` (approved design)
**Status:** For review — no code written yet
**Principle:** TDD per phase (test-first, watch fail, implement, green); each phase
ships green (pytest + ruff + mypy --strict) and is committed before the next.
The new engine is **flag-gated** (default = Ralph) so nothing existing breaks
until we flip it.

---

## Layout

New cohesive subpackage `src/stromboli/engine/`, leaving the Ralph `loop.py`
untouched and selectable:

```
src/stromboli/engine/
  __init__.py        exports
  state.py           BuildState, Unit, Verdict, Plan + atomic disk I/O
  actions.py         Action union + terminal reasons
  policy.py          ControlPolicy protocol + DeterministicPolicy (pure)
  gate.py            Objective Gate (code: run tests/lint/types in worktree)
  orchestrator.py    GraphEngine: the loop (policy → node → persist), breaker-bounded
  result.py          BuildResult protocol (shared by Ralph + graph)
  nodes/
    __init__.py
    base.py          AgentNode seam: build_prompt / invoke (injected CC) / parse (fail-closed)
    schemas.py       pydantic models: PlanModel, VerdictModel, FeedbackModel
    planner.py       Planner node
    worker.py        Worker node
    verifier.py      Verifier node
    reflector.py     Reflector node
tests/engine/
  test_state.py  test_policy.py  test_gate.py  test_orchestrator.py
  test_nodes_planner.py  test_nodes_worker.py  test_nodes_verifier.py  test_nodes_reflector.py
```

On-disk state (in the worktree, git-tracked): `.stromboli/state.json`,
`.stromboli/feedback.md`, `.stromboli/transcripts/`.

---

## Phase 0 — Result abstraction (enabling refactor)

**Why first:** `pipeline.run_build` currently consumes a `LoopResult`
(`.tripped`, `.trip`, `.prd_path`, `.iterations`). Both engines must feed the
same Integrator/write-back, so extract a tiny shared shape.

- **New** `engine/result.py`: `BuildResult` Protocol — `reason`, `tripped`,
  `trip`, `blocked_items()`, `completed_count()`, `artifact_path`,
  `usage_spans()`.
- **Change** `loop.LoopResult` to satisfy it (additive; no behavior change).
- **Change** `pipeline._finalize` / `_integrate` to type against `BuildResult`.
- **Tests:** existing 139 stay green; add `test_result_protocol` (LoopResult
  conforms).
- **Acceptance:** no behavioral change; green.

## Phase 1 — State + Actions + Policy (the pure heart, no agents)

- **New** `state.py`:
  - `Unit(id, description, acceptance_criterion, definition_of_done, attempts,
    status: PENDING|VERIFIED|BLOCKED, last_verdict)`.
  - `Verdict(scope, met, hollow_tests, reasons)`; `Plan(version, units)`.
  - `BuildState(plan, feedback, replan_count, history, root)` + `save()`/`load()`
    (atomic temp+rename) + helpers: `next_eligible_unit()`, `all_verified()`,
    `blocked_units()`, `record(step)`.
- **New** `actions.py`: `RunPlanner`, `RunWorker(unit_id)`,
  `RunVerifier(scope)`, `Reflect(unit_id)`, `Replan(reason)`, `Integrate`,
  `RouteToReview(reason)`.
- **New** `policy.py`: `ControlPolicy` Protocol (`decide(state) -> Action`);
  `DeterministicPolicy` implementing the rules; `_no_progress(state, unit)`
  detector (repeated diff-hash / unchanged verdict reasons); re-plan counter cap.
- **Tests (the centerpiece):** `test_state` (round-trip, atomic write, resume
  from disk); `test_policy` — exhaustive table over every branch: no plan→Plan;
  eligible unit→Worker; gate fail→Worker; verify reject→Reflect; reflect says
  replan→Replan (counter-bounded); all verified→whole-diff Verify; whole pass→
  Integrate; K exhausted→blocked→(any blocked)→RouteToReview (D10); breaker
  pre-empt→RouteToReview; no-progress→RouteToReview.
- **Acceptance:** policy is pure + 100% branch-covered; green.

## Phase 2 — Node contracts (agents behind seams)

- **New** `nodes/schemas.py`: pydantic `PlanModel`, `VerdictModel`,
  `FeedbackModel` (strict; extra=forbid).
- **New** `nodes/base.py`: `AgentNode` — `build_prompt(state, target) -> str`,
  injected `CCRunner` (reuse `loop.CCRunner`), `parse(stdout) -> model`
  with **fail-closed**: invalid JSON / schema → one re-prompt with schema echoed
  → still bad → raise `NodeOutputError` (policy treats as fail).
- **New** `planner.py` / `worker.py` / `verifier.py` / `reflector.py`: each a
  prompt template + typed parse. Worker writes to the worktree; Verifier returns
  a `Verdict`; Reflector returns feedback or a replan signal.
- **Tests:** per-node contract — prompt contains the right state inputs
  (criterion, diff, feedback); parse of valid / malformed(→fail-closed) /
  schema-reject; never auto-pass on garble. Fake CC runner, no real `claude`.
- **Acceptance:** green; no network.

## Phase 3 — Objective Gate + Integrator adapter

- **New** `gate.py`: `ObjectiveGate(commands, run)` →
  `GateResult(passed, output)`. Default commands = `uv run pytest -q` / `ruff` /
  `mypy`, **configurable per target repo** (some repos differ); inject a command
  runner. Distinguishes tooling crash from test failure (Section 5.4).
- **Reuse** existing `publish_pr` / `route_task` / `resilient_append` /
  `record_build_trace` via a thin `engine` integrate path consuming `BuildResult`.
- **Tests:** `test_gate` (pass/fail/tooling-crash branches with a fake runner);
  integrator adapter reuses existing covered code.
- **Acceptance:** green.

## Phase 4 — Orchestrator

- **New** `orchestrator.py`: `GraphEngine(policy, nodes, gate, breaker_config,
  tracer, max_replans).run(worktree_root) -> BuildResult`. Loop:
  `while not terminal and breaker_ok: action = policy.decide(state); dispatch
  node; persist state; record Langfuse span`. Breaker records every agent cost;
  on ceiling forces `RouteToReview`.
- **Tests:** `test_orchestrator` — full graph with **fake agents** scripting
  verdicts: happy path, reflection path, replan path, breaker trip, K-exhaustion
  → Review, and **resume-from-disk** golden fixture. Plus the **forward-compat
  check**: a fake `SupervisorPolicy` drives the same nodes (proves D7).
- **Acceptance:** green; deterministic.

## Phase 5 — Engine selection flag + app wiring

- **Change** `settings.py`: add `STROMBOLI_ENGINE` (`ralph` default | `graph`)
  and breaker overrides if desired.
- **Change** `pipeline.run_build` / `app.build_deps`: select Ralph vs GraphEngine
  by the flag; both produce a `BuildResult` consumed by the same finalize.
- **Tests:** `test_app` — flag selects engine; default stays Ralph; graph path
  wired with fakes.
- **Acceptance:** green; default behavior unchanged.

## Phase 6 — Docs + live smoke (manual, behind flag)

- README: document `STROMBOLI_ENGINE=graph`, the node roles, and the per-repo
  gate config.
- Manual smoke: run one real Notion task through `graph` in a sandbox repo;
  inspect the Langfuse trace (planner/worker/verifier/reflector spans) and the
  resulting PR. No auto-merge; human reviews.
- **Acceptance:** a real task produces a verified PR (or a Review with coherent
  feedback) end-to-end.

---

## Sequencing & risk

- Phases 0→1→2→3→4→5 are strictly ordered (each builds on the prior).
- **Highest-value, lowest-risk first:** Phase 1's `DeterministicPolicy` is the
  brain and is pure/fully testable — most of the correctness guarantee lands
  before a single agent call exists.
- **Reversibility:** every phase is flag-gated and additive; Ralph remains the
  default and fully functional until we deliberately flip `STROMBOLI_ENGINE`.
- **Estimated new surface:** ~10 source modules + ~8 test modules; comparable to
  the original US-005..US-013 build.

## Open items to confirm before/while building

1. Per-repo **gate command config** — where does a target repo declare its
   test/lint/type commands? (proposed: a `stromboli.toml` in the target repo,
   default to uv/pytest/ruff/mypy.)
2. **Unit cap** value for plan-explosion guard (proposed: 15).
3. **Re-plan counter** cap (proposed: 2).
4. Whether Phase 0's `BuildResult` refactor should also retire `prd.py`'s
   regex compile now, or leave it dormant until `graph` is default.
