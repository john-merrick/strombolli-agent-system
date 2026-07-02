# Design: self-improving architecture

**Status:** shipped (2026-07-03) · **Owner:** Isaac

Four loops that turn Stromboli from "learns lessons in memory" into a system
that improves its own judgment and skills over time. They have a hard
dependency order — the failure pipeline is the foundation the others read.

## §1 Failure-to-dataset pipeline (foundation)

Every terminal verdict (pass *and* rejection) is captured at the graph's
terminal boundary into a local SQLite store, `.stromboli/failures.db`
(`orchestration/failure_index.py`), via an `on_terminal` seam on `GraphDeps`.
Previously the verifier's structured rejection signal
(`expected/observed/cause/fix/task_type/failure_mode`) was distilled durably
only on a verified *pass*; for rejected/escalated runs it lived only in the
unindexed per-task `trace.jsonl` and was discarded.

`FailureIndex`: `record` (idempotent, last-write-wins, diff/evidence truncated),
`label` (human accept/reject on the verifier's call), `unresolved` (the
backlog's input), `labelled` (the GEPA trainset), `export_verifier_dataset`
(→ the eval-harness JSON `load_dataset` reads). Best-effort — capture never
crashes a run.

## §2 GEPA on the verifier (the judge)

The verifier is the highest-leverage prompt, so it is made optimizable without
touching LangGraph. `_SYSTEM` → `DEFAULT_VERIFIER_SYSTEM` + an injectable
`system_prompt` param on `make_verifier`. `verifier_predictor` adapts a labelled
case into the real verifier call (the production predictor the harness always
expected). `observability/evals/verifier_optimize`: `evaluate_prompt` /
`select_best_prompt` score candidates against the labelled set — baseline wins
ties, so a prompt is adopted only if it *strictly* beats the current judge —
plus `gepa_candidates` behind a lazy DSPy import (`[optimize]` extra, offline
only). `stromboli optimize-verifier` exports the trainset and scores the current
prompt. **Adoption is always a human decision, never automatic.**

## §3 Skill library the agent writes to

A resolved-with-divergence pass distills a reusable *skill* ("what worked", the
validated fix as forward guidance) into procedural memory. To honour "a bad
skill can't silently degrade the system", a skill enters as an unvetted
**candidate** and is never injected into the coder until an eval promotes it to
**approved** (`ProceduralMemory` candidate/approved status + `promote`;
`Memory.recall_skills` returns approved-only, bounded top-k). The planner
injects approved skills via a `skill_retriever` seam. `evals/skill_gate.gate_skill`
A/Bs a candidate over `coding_eval` (real coder+sandbox pass-rate, skill off vs
on) and promotes only on no-regression.

## §4 Morning rundown → improvement backlog (capstone)

`orchestration/rundown.py` clusters unresolved failures by
`task_type × failure_mode` and routes each cluster: missing knowledge → memory;
a human *reject* (the judge was wrong) → the GEPA queue; structural/architectural
→ a ticket. Pure functions (`cluster_failures`, `route_for`, `format_digest`,
`format_backlog`); the `stromboli rundown [--notify]` subcommand prints the
routed digest, writes ticket clusters to `backlog.md`, and optionally pushes the
digest to Telegram.

## Dependency order & operation

Build/run order: §1 (foundation) → §3 ∥ §2-scaffold → §4. In production the
watcher records every run into `failures.db`; run `stromboli rundown` (manually
or scheduled) for the digest, and `stromboli optimize-verifier` once labelled
volume accrues.

## Future extension (out of scope)

Wire the DSPy GEPA program in `gepa_candidates` once the labelled trainset has
volume; feed rundown's "ticket" route into a real issue tracker; add a
lesson/skill effectiveness signal (did injecting it reduce revises?) to decay
unhelpful entries — the closest a frozen system gets to a measured gradient.
