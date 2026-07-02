# Design: context as mutable state — surprise within, lessons across

**Status:** accepted (2026-07-03) · **Owner:** Isaac · **Facilitated:** brainstorming session

## Understanding summary

- A two-layer "context-as-mutable-state" discipline. **Within-episode** (the
  outer revise cycle): the verifier emits *structured surprise* — expected X /
  observed Y / cause Z / fix — and only that compressed delta is injected into
  the next coder pass, instead of the raw verifier `reason`. **Across-episode:**
  those structured verdicts are distilled into durable, retrievable **lessons**
  (ChromaDB episodic, tagged `task_type` + `failure_mode`) and injected into the
  **planner (prompt node)** at the start of future runs.
- Why: a frozen-weight system can't learn in weights; curated context is its
  only mutable state. Compress surprise within a run, distill+retrieve lessons
  across runs — the cheapest path to test-time adaptation, and a sharpening of
  machinery that already exists but is blunt (raw-reason reflections,
  semantic-only recall at Spec).
- Who: the autonomous Stromboli loop; the operator as reviewer.
- Constraints: never reimplement the SDK agent loop (so "within-episode" = the
  outer revise cycle we control); reuse ChromaDB; memory stays best-effort;
  near-zero added cost (Verdict extension = no new LLM calls).
- Non-goals (v1): touching the SDK's mid-loop turns; a new store / Notion DB;
  throughput work; the lesson-effectiveness feedback loop (noted as the next
  frontier extension).

## Assumptions

1. `Verdict` gains `expected`, `observed`, `cause`, `fix`, `task_type`,
   `failure_mode` (default `""`); the verifier fills them — zero new calls.
2. Lessons are stored only from runs that **resolved** (a `pass`, incl. one
   reached via revises / the PR feedback loop) so a lesson carries a *validated*
   remedy, reducing context poisoning.
3. v1 retrieval is semantic similarity on the task goal, filtered to
   `kind="lesson"`, top-k small; `task_type`/`failure_mode` ride as metadata for
   optional stricter filtering later (deferred, YAGNI).
4. Lesson injection moves to the prompt node (the planner), replacing Spec-time
   raw-reflection recall; Spec keeps lighter conventions recall.
5. Raw free-text reflection *recall* is retired for structured lessons;
   reflections still persist for the run trace / audit.
6. Recency/decay via the existing `ts`; unbounded lesson growth is a slow,
   deferred concern (retrieve-don't-accumulate keeps reads bounded).

## Decision log

| Decision | Alternatives | Why |
|---|---|---|
| Within-episode = the outer revise cycle | breach SDK boundary; initial-context only; drop it | the SDK owns the inner loop; the revise cycle is the seam we control |
| Store lessons in ChromaDB episodic + metadata | new collection; Notion DB | reuse wired machinery; no new infra |
| Extend `Verdict` schema | dedicated distill call; hybrid | zero new LLM calls; one source of truth |
| Inject lessons at the planner (prompt node) | Spec only; both | lands closest to where instructions are authored |
| Store only resolved-with-divergence lessons | store failures too | a lesson must carry a validated fix, not a complaint |
| Last `pass` verdict is the stored lesson | distill whole arc | one crisp record; supersedes intermediate wrong guesses |
| Semantic top-k=3 filtered to `kind="lesson"` | strict metadata filter now | simplest that captures most value; harden later if noisy |

## Design

### Schema (`state.py`)
`Verdict` gains `expected/observed/cause/fix/task_type/failure_mode` (all
default `""`, back-compatible). A **lesson** document is rendered from a verdict:
`"When {task_type} and {failure_mode}: expected {expected}, but {observed};
cause {cause}; fix {fix}."` with metadata `{task_id, kind:"lesson", task_type,
failure_mode, ts, resolved:true}`.

### Within-episode (`nodes/coding.py`)
On a `revise`, `_build_prompt` renders a directive block — Expected / Observed /
Cause / Do-this(`fix`) — instead of the raw `reason`; falls back to `reason`
when the fields are empty. The SDK session is resumed as today; only the
injected *delta* changes. `fix`-led + directive to cut re-exploration.

### Across-episode (`nodes/memory.py`, `memory/episodic.py`)
`EpisodicMemory.record_lesson(...)` writes `kind="lesson"` with the tags. The
memory-write node writes one lesson only when the run resolved (`pass`) *and* the
final verdict carries divergence (`cause`/`fix` non-empty). Traces still written
on pass; reflections still persisted; raw-reflection *recall* retired.

### Retrieval + injection (`memory/__init__.py`, `nodes/prompt.py`)
`Memory.recall_lessons(goal, k=3)` queries episodic filtered to `kind="lesson"`.
The prompt node gains an optional `retriever` seam (mirroring Spec's) that
prepends a "Lessons from past similar tasks (avoid repeating these)" block to the
planner context. Off when memory is unwired. Wired via `deps.memory.recall_lessons`
in `graph._deps_from_settings` and `TriagePhases`.

### Edge cases
Empty surprise → fall back to `reason` / write no lesson. Poisoning mitigated by
store-only-resolved + `kind` filter + small k + recency. Loose retrieval bounded
by k=3 and "use if relevant" phrasing; harden to metadata filter if noisy (no
migration). Memory errors best-effort/swallowed. New fields default `""` → all
existing fakes/evals/stubs untouched.

### Testing
Verdict round-trips new fields; `_build_prompt` renders block + falls back;
memory-write writes a lesson only on resolved-with-divergence, not on
clean-pass/escalate; `record_lesson` tags correctly; `recall_lessons` filters to
`kind="lesson"`; prompt node injects retrieved lessons and no-ops without memory.
pytest + ruff + mypy --strict green.

### Future extension (out of v1)
A third loop: measure whether an injected lesson reduced revises/turns on the
next matching task, and decay lessons that don't help — the closest a
frozen-weight system gets to a real gradient step (an update judged by its
effect).
