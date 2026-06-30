# Design — Stromboli interactive escalation resolver (the "investigate loop")

Status: **Design (validated in brainstorming)** — not yet implemented.
Scope: **Lane B only** (task-driven escalations). Lane A (system/harness errors) is
out of scope.

## 1. Understanding summary
- **What:** an interactive, Telegram-based escalation resolver. When a task escalates
  for a *task-driven* reason (ambiguous spec, or repeated verifier/quality rejection),
  instead of a dead-end "🚨 escalated" ping, an **Investigator agent** opens a
  back-and-forth chat, helps you understand the run, takes your guidance, and
  **resumes the same suspended run** with that guidance.
- **Why:** today's escalation is one-way and terminal — recovery means manually opening
  the repo. This closes the loop so most task escalations resolve in a short chat.
- **Who:** single operator (you); a personal autonomous-coding pipeline.
- **In scope:** durable pause + resume-in-place; an Investigator with read + sandbox
  probe (no edits); explicit human approval before resume; concurrent paused tasks with
  per-task `#N` routing on a dedicated bot.
- **Non-goals:** Lane A system/harness errors (keep alert + Review, manual debug); the
  agent editing/committing fixes itself; bypassing the verifier gate; auto-resume
  without approval; rate-limit/transient handling (already an auto-resume concern).
- **Invariants preserved:** the coder's only oracle stays the sandbox; the independent
  verifier gate judges every resumed attempt; the investigator probes read/execute only
  and never self-judges.

## 2. Assumptions
1. **Channel:** dedicated **`strombolli-investigate-bot`**, bidirectional via `getUpdates`
   long-poll. Token → `op://Dev-Secrets/strombolli-investigate-bot/credential`,
   id → `op://Dev-Secrets/strombolli-investigate-bot/username`. The existing bot keeps
   fire-and-forget status pings.
2. **Routing:** short `#N` handles (small integers) map to `task_id` in the paused index.
3. **Approval:** the agent posts a guidance summary; an explicit `✅` resumes via
   `Command(resume=…)`, re-running coding→verifier.
4. **State:** distinct Notion status **"Queued"** (one-time manual add; non-dispatchable);
   durable on-disk **SqliteSaver** checkpoints keyed `thread_id=task_id`.
5. **Security:** single authorized `chat_id`; artifact content treated as untrusted data;
   guidance never bypasses the verifier.
6. **Resource hygiene:** suspended runs hold worktree/`session_id`; **3-day** expiry →
   Review + worktree cleanup.
7. **Scale:** low volume — design for ~5 concurrent paused tasks; investigator cost is
   per-conversation only.

## 3. Architecture & components
**Modified**
1. **Triage graph** — Lane-B escalate path calls **`interrupt()`** (not finalize→Review).
   Checkpointer: `MemorySaver` → durable **`SqliteSaver`** (`thread_id=task_id`).
2. **Prefect triage flow** — on `__interrupt__`: set Notion **Queued**, write paused-index
   row, assign `#N`, send the opener on investigate-bot, return (free the `limit=1` slot).
3. **Existing notifier bot** — keeps 👀/🆕/✅/🚨 status pings.

**New**
4. **`investigate-serve`** (standalone, `stromboli investigate-serve`) — long-polls the
   investigate-bot, routes inbound by `#N`, hosts the Investigator agent, and on `✅`
   resumes the graph directly via `Command(resume=…)`.
5. **Investigator agent** — LiteLLM **reasoning** surface (not coder, not verifier).

**Supporting**
6. **Paused-task index** (SQLite table): `task_id, ref(#N), reason, worktree_path,
   session_id, paused_at, chat/thread, transcript, state(open|resumed|expired)`.
7. **Expiry sweeper** — 3-day TTL → Review + cleanup.

## 4. State model & lifecycle
Three stores, distinct roles:
- **SqliteSaver checkpoint** — graph state to resume (authoritative for execution).
- **Paused-task index** — routing + expiry metadata (authoritative for "resumable / who").
- **Notion status** — human-visible label only.

```
To do / Working on
   │  (Lane-B escalation: ambiguous spec / verifier reject / revision cap)
   ▼
QUEUED ──(your ✅ + guidance)──▶ Working on ──▶ Complete (PR)
   │                                          └─▶ Review (hand to human)
   ├─(3-day expiry)──▶ Review  (+ worktree cleanup)
   └─(/drop #N)──────▶ Review  (+ cleanup)
```
`Queued` is a one-time manual Notion status add; the dispatch guard treats it as
non-dispatchable so the poll never re-grabs a mid-conversation task.

## 5. Data flow (happy path)
1. **Escalate → suspend.** Escalate node `interrupt({task_id, reason, artifact refs})`;
   SqliteSaver persists the checkpoint.
2. **Prefect reacts.** `__interrupt__` → Notion **Queued** → index row + next free `#N` →
   opener on investigate-bot → return (free slot).
3. **Converse.** `investigate-serve` long-polls; routes by `#N`; the Investigator agent
   loads the run record (spec, verdict, diff, test output, trace) and may probe the
   worktree (re-run tests, grep). Multi-turn until aligned.
4. **Approve.** Agent posts a one-line guidance summary; you reply `✅`.
5. **Resume in place.** `graph.invoke(Command(resume=<guidance>), thread_id=task_id)`
   re-enters at the interrupt, folds guidance into spec/prompt, re-runs coding→verifier.
   Notion **Queued → Working on** → terminal (Complete + PR, or Review). Index row closes;
   `#N` frees.

Routing: `#N` in every message + `/queued`; no `#N` & one open → unambiguous; several
open → bot asks which. Heavy coder work on resume runs **in the service** (Approach-A
tradeoff: not shown in Prefect UI; Approach B can add that later without rework).

## 6. Investigator agent
- **Surface:** LiteLLM reasoning; per-task message history.
- **Context:** spec, verdict (decision+reason), git diff, last sandbox test output,
  reflections, trace ref — all from existing artifacts.
- **Tools (bounded, fail-closed):** read worktree files, grep, view `git diff`, re-run the
  test command via the sandbox runner. **No writes/commits/PR/verifier calls/edits.**
- **Output of record:** a **guidance string** + a summary for your `✅`. That string is the
  only thing that crosses back into resume — no action smuggled past the verifier.
- **Bounds:** per-conversation turn/token cap (`MAX_INVESTIGATE_TURNS` analog); bounded
  probes.
- **Untrusted input:** artifact text is data, not instructions; worst case is bad guidance
  the verifier still judges.
- **Durability:** transcript persisted in the index row → survives service restart.

## 7. Telegram routing, auth & security
- **Channel:** investigate-bot `getUpdates` (persisted offset) + `sendMessage`.
- **Auth:** single authorized `chat_id`; others ignored + logged.
- **Commands:** `#N <msg>` · `/queued` · `✅`/`go` · `/drop #N`. Single-open inference;
  multi-open prompt.
- **Secrets:** `op://` refs only; never plaintext on disk.

## 8. Error handling & edge cases
- **Service restart:** offset + transcript + index rehydrate conversations/queued tasks.
- **Watcher/Prefect restart:** SqliteSaver checkpoints survive → resumable.
- **Re-escalation after guidance:** one fresh revision granted; a second verifier
  rejection → Review (human), never another Queued loop.
- **Resume error / worktree gone:** graceful → 🚨 + Review + close index; no loop.
- **3-day expiry:** sweeper → Review + cleanup; late reply → "#N expired, parked to Review."
- **Bad/duplicate input:** unknown/closed `#N` → friendly error + `/queued`; second `✅`
  is idempotent.
- **Rate-limit during resumed coder run:** existing retryable-escalation (preserve
  `session_id`) unchanged.

## 9. Testing strategy
Unit tests with injected fakes (no real Telegram/LLM/Docker/git):
- escalate interrupts for Lane-B; Prefect maps `__interrupt__` → Queued + index + `#N`.
- dispatch guard excludes Queued.
- paused index: `#N` assign/free, transcript persistence, expiry selection.
- routing: `#N` parse, single-open inference, multi-open prompt, unknown-`#N`.
- investigator: fake gateway; asserts only allowed tools used; emits guidance; injection
  content can't escalate privileges.
- resume: fake checkpointer → `Command(resume=…)` threads guidance into spec/prompt.
- expiry sweeper; `chat_id` auth rejection.

Each phase ships green (pytest + ruff + mypy --strict), per CLAUDE.md.

## 10. Decision log
- **DL-1.** Escalations split into two human-facing lanes (A: system/platform; B:
  task-driven) + an auto lane (transient/rate-limit). *Alt: one unified handler — rejected;
  end-states differ too much.*
- **DL-2.** Lane B = durable pause + resume in place. *Alt: terminate + re-run fresh
  (loses session/worktree); hybrid grace window (most moving parts) — rejected.*
  **Mechanism (revised at implementation):** the Prefect runtime runs `TriagePhases`
  (plain `state→state` steps), **not** the compiled LangGraph, so it has no
  `interrupt()`/checkpointer. Suspend/resume is therefore done by **persisting the
  `StromboliState`** (Pydantic; already carries `session_id`) into the paused index;
  the worktree persists on disk keyed by `task_id`. Identical behavior, fits the
  runtime, keeps the per-node Prefect UI, simpler than SqliteSaver. SqliteSaver is
  no longer required.
- **DL-3.** Investigator = read run record + probe sandbox; no resume without approval; no
  self-fix. *Alt: read-only briefer (too thin for code-quality); co-fixer (re-does coder,
  blurs the oracle) — rejected.*
- **DL-4.** Paused runs release the worker (concurrent pauses) → needs per-task routing +
  by-`task_id` resume. *Alt: block one-at-a-time — rejected, one stuck escalation stalls
  everything.*
- **DL-5 / DL-7.** Lane A dropped (not just deferred): "no sys ops." Build Lane B only;
  system errors keep alert + Review + manual debug. Investigator never reads Stromboli's
  own source — only run artifacts + the task worktree.
- **DL-6.** Dedicated `strombolli-investigate-bot` for the back-and-forth; existing bot
  keeps status pings (two-bot split keeps noise and dialogue separate).
- **DL-8.** Pause auto-expiry = 3 days → Review + cleanup. *Alt: 7 days; no expiry —
  rejected (worktrees pile up).*
- **DL-9.** Receiver/resume = separate standalone service. *Alt: inside watcher (couples
  concerns); as a Prefect flow (bends scheduled-run model) — rejected.*
- **DL-10.** Approach A — the standalone service drives resume directly. *Alt: B (resume
  via Prefect, more plumbing now) — deferred as clean future add; C
  (investigator-as-subgraph) — rejected, heaviest.* **Resume re-enters the phase loop**
  by loading the persisted `StromboliState` and merging the guidance (see DL-2 revised
  mechanism) — not `Command(resume=…)`, since Prefect doesn't run the graph.
- **DL-11.** New Notion status named **"Queued"** (one-time manual add; non-dispatchable).
- **DL-12.** Short `#N` handles map to `task_id`; not reused while open; simple.
- **DL-13.** Re-escalation: a human-guided resume grants one fresh revision; second
  verifier rejection → Review, never another Queued loop.
- **DL-14.** Security: single `chat_id` authN lock; persisted offset + transcript for
  restart safety; artifact content untrusted; no resume bypasses the verifier.

## 11. Open follow-ups (non-blocking)
- Prefect-UI visibility of resumed runs (Approach B) — add later if missed.
- `MAX_INVESTIGATE_TURNS` / token budget exact values — tune during implementation.

## 12. Implementation status (Phases 1–5 shipped, green)
All five phases are built and tested with injected fakes:
1. Suspend foundation (Queued status, PausedIndex, phases.suspend, dispatch guard).
2. Receiver + investigate-serve skeleton (auth, command grammar, routing, loop).
3. Investigator agent + `/retest` probe.
4. Resume path (`resume_with_guidance`, one-fresh-revision, `make_resumer`).
5. Expiry sweeper, `serve_from_settings` factory, `stromboli investigate-serve`
   CLI, `_deps_from_settings` wiring (paused_index + investigate opener), env vars.

### Phase 6 — durable worktrees (shipped, green)
The worktree-persistence gap is now closed, so resume/probe rebuild real code:
- `WorktreeManager.ensure()` — durable, idempotent provisioning (no auto-remove;
  a re-call reuses the existing worktree, keeping the coder's prior work) — plus
  `remove()` for explicit teardown.
- `_deps_from_settings` wires a durable `worktree_for` (ensure-based) + a
  `worktree_cleanup` remover into the phases/Prefect path (so the Prefect watcher
  now does real coding, not the stub; `run_task` still overrides with its own
  context-managed provisioning).
- `phases.finalize` frees the worktree on a terminal outcome; **suspend does
  not**, so a Queued task's worktree survives for resume/probe; the expiry sweeper
  removes it via its `cleanup` hook.
- `triage_flow` guards the build: an unbuildable task (e.g. no repo) parks to
  Review instead of crash-looping the poll.

Net: the investigate loop is end-to-end on the Prefect path. Remaining
nice-to-haves are still §11 (Prefect-UI visibility of resumes; turn/token tuning).
