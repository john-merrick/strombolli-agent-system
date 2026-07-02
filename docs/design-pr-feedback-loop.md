# Design: the PR feedback loop (reality closes the outer recursion)

**Status:** accepted (2026-07-02) · **Owner:** Isaac · **Facilitated:** brainstorming session

## Understanding summary

- An opened PR becomes a **checkpoint, not the finish line**: failing CI runs
  and human PR review comments automatically drive a bounded fix cycle that
  resumes the same coder session, re-verifies with Gemini, and pushes to the
  same branch.
- Exists because the loop previously ended at "PR opened" — CI failures and
  review feedback required re-prompting a human operator.
- For the single operator (reviewer/merger) on the always-on Mac Mini.
- Trigger is **polling** (existing ops model, no inbound networking): the
  watcher sweeps Stromboli-opened PRs on its poll cadence (~every 4th pass,
  ≈2 min).
- **Bounds:** max 2 automatic fix rounds per PR, then escalation. Merging is
  strictly human.
- **Non-goals:** auto-merge; webhooks; parallel workers; merge-outcome memory
  learning (deferred).

## Assumptions

1. Target repos may or may not have GitHub Actions; without CI the loop is
   comments-only.
2. Only `stromboli/*` branches are watched, tracked in a persistent index.
3. Fix cycles reuse existing machinery: worktree from the PR branch, coder
   `session_id` resume, sandbox oracle, verifier gate, token ceiling.
4. The operator comments from the same GitHub account as `GITHUB_TOKEN`, so
   comment filtering is **marker-based**, not author-based: Stromboli's own PR
   comments carry a `🌋 stromboli:` prefix and are ignored; a human comment
   containing `stromboli: skip` opts the PR out of the loop.
5. Single-digit open PRs; API rate limits are a non-issue at this cadence.
6. Security unchanged (existing token scope, fail-closed tools, sandboxed
   tests, never force-push over human work).

## Decision log

| Decision | Alternatives | Why |
|---|---|---|
| Reality loop first | cost, robustness, throughput | operator picked highest leverage |
| CI + review comments | CI-only; CI+comments+merge-learning | middle scope; learning deferred |
| Poll open PRs | webhooks; Actions callback | no public endpoint on the Mac; one ops model |
| 2 fix rounds, human merges | 3–5 rounds; auto-merge | bounded spend; PR = the hard human gate |
| Sweep inside the watcher (A) | third service (B); investigate-fold (C) | serial token usage; no cross-process state (the double-dispatch lesson); separation of concerns |
| Re-verify every fix | push unverified | the verifier gate is the point |
| Marker-based comment filtering | author-based | operator and bot share one token/account |

## Components

- `orchestration/pr_index.py` — SQLite watch index at
  `<WORKSPACE_ROOT>/.stromboli/prs.db`:

  ```
  pr_watch(task_id PK, repo, branch, pr_number, pr_url, goal, session_id,
           fix_rounds, last_ci_sha, last_comment_at, state, updated_at)
  state ∈ watching | fixing | escalated | closed
  ```

- `orchestration/pr_feedback.py` — `sweep()` called by `stromboli watch` after
  the Notion drain (every 4th pass). Registration at the source: the PR node
  records the PR via an `on_published` seam on `GraphDeps` after a live
  publish.

## Sweep flow (per watched PR)

1. Pull state → merged/closed → mark `closed`, stop watching.
2. **CI signal**: check-runs for head SHA; act only when SHA ≠ `last_ci_sha`
   and a conclusion is `failure` (pending → wait). Extract failing job log
   tail (Actions API; fall back to check-run summary/annotations).
3. **Comment signal**: issue + review comments after `last_comment_at`,
   ignoring `🌋 stromboli:`-marked ones; `stromboli: skip` opts out.
4. Signal + `fix_rounds < 2` → fix cycle; else escalate: Notion → Review,
   Telegram, `🌋 stromboli:` PR comment, index `escalated`.

## Fix cycle

Worktree ensured **from the PR branch** (origin is the source of truth) →
prompt = original goal + `# CI failure` log tail or `# Reviewer feedback`
comments + fix-and-test instruction → `coder.run(resume=session_id)` →
sandbox tests → **Gemini verifier** → pass → `commit_and_push` (same branch;
PR updates in place). Update rounds/session/watermarks. Verifier non-pass or
provisioning failure → escalate. Each cycle = one Langfuse trace
(`fix: <task> — round N`, `task_id` correlation).

## Edge cases

- Ping-pong bounded by rounds (a Stromboli push that fails CI again is round 2,
  then escalation).
- Branch deleted / human force-push → provision failure → escalate; never
  overwrite human work.
- No CI → comments-only, silently.
- Rate limit mid-fix → existing retryable escalation preserving the session.
- Crash mid-fix → `fixing` staler than 30 min resets to `watching`; re-entry
  is idempotent.
- Comments during a fix → timestamp watermark catches them next sweep.

## Testing

Unit-level with fakes (GitHub gateway, coder, tmp index): red CI → exactly one
fix; comment → fix; marker/skip filtering; rounds exhaustion → escalate;
merged → cleanup; stale `fixing` recovery; branch-gone → escalate. Live
validation at rollout on a real Stromboli PR. pytest + ruff + mypy --strict
stay green.
