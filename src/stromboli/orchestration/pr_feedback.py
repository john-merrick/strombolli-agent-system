"""The PR feedback loop — reality (CI + review comments) closes the loop.

An opened PR is a checkpoint (design: docs/design-pr-feedback-loop.md). On each
sweep, for every watched PR:

* merged / closed → stop watching;
* a **new** failing CI run (head SHA we haven't acted on) → fix cycle;
* **new human review comments** → fix cycle (Stromboli's own ``🌋 stromboli:``
  comments are ignored; a ``stromboli: skip`` comment opts the PR out);
* a fix cycle resumes the coder ``session_id``, re-runs the sandbox oracle,
  re-verifies with the (non-Claude) verifier, and pushes to the same branch —
  bounded by :data:`MAX_FIX_ROUNDS`, after which it escalates.

The fix cycle reuses the graph's own nodes (``make_coding`` / ``make_verifier``)
against a worktree checked out from the PR branch, so the coder/sandbox/verifier
behaviour is identical to a first build. Merging stays human — this loop never
merges.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from stromboli.integrations.github import commit_and_push
from stromboli.integrations.notion import Repo
from stromboli.nodes.coding import make_coding
from stromboli.nodes.verifier import make_verifier
from stromboli.orchestration.pr_index import (
    CLOSED,
    ESCALATED,
    FIXING,
    WATCHING,
    PRIndex,
    PRWatch,
)
from stromboli.state import Spec, StromboliState, Verdict

logger = logging.getLogger(__name__)

#: Comments Stromboli posts carry this prefix so the sweep ignores its own voice.
STROMBOLI_MARKER = "🌋 stromboli:"
#: A human comment containing this opts the PR out of the loop.
SKIP_MARKER = "stromboli: skip"
#: Max automatic fix rounds per PR before escalation (design decision).
MAX_FIX_ROUNDS = 2


def _relevant_comments(comments: list[Any]) -> list[Any]:
    """Human comments only — drop Stromboli's own marked ones."""
    return [c for c in comments if STROMBOLI_MARKER not in c.body]


def _fix_feedback(ci_logs: str, failing: tuple[str, ...], comments: list[Any]) -> str:
    """Assemble the reviewer-feedback block injected into the fix prompt."""
    blocks: list[str] = []
    if ci_logs or failing:
        header = "# CI failure"
        if failing:
            header += f" ({', '.join(failing)})"
        blocks.append(f"{header}\n{ci_logs or '(no log detail available)'}")
    if comments:
        joined = "\n\n".join(f"- {c.author}: {c.body}" for c in comments)
        blocks.append(f"# Reviewer comments (address these)\n{joined}")
    return "\n\n".join(blocks)


class PRFeedbackService:
    """Sweeps watched PRs and runs bounded fix cycles. Seams injected for tests."""

    def __init__(
        self,
        *,
        index: PRIndex,
        github: Any,
        coder: Any,
        sandbox: Any,
        gateway: Any,
        verifier_model: str,
        worktree_manager: Any,
        now: Callable[[], str],
        on_escalate: Callable[[PRWatch, str], None],
        stale_fixing_cutoff: Callable[[str], bool] | None = None,
    ) -> None:
        self._index = index
        self._github = github
        self._coder = coder
        self._sandbox = sandbox
        self._gateway = gateway
        self._verifier_model = verifier_model
        self._wm = worktree_manager
        self._now = now
        self._on_escalate = on_escalate
        self._is_stale = stale_fixing_cutoff or (lambda _updated: False)

    def sweep(self) -> list[str]:
        """One pass over all active PRs. Returns the task_ids acted on."""
        acted: list[str] = []
        for watch in self._index.active():
            try:
                if self._sweep_one(watch):
                    acted.append(watch.task_id)
            except Exception:  # noqa: BLE001 - one bad PR must not kill the sweep
                logger.exception("PR sweep failed for %s", watch.task_id)
        return acted

    def _sweep_one(self, watch: PRWatch) -> bool:
        repo = Repo(*watch.repo.split("/", 1))
        # A crashed fix (left 'fixing') past the staleness window is recoverable.
        if watch.state == FIXING and not self._is_stale(watch.updated_at or ""):
            return False

        pr = self._github.get_pull_request(repo, watch.pr_number)
        if pr.state == "closed" or pr.merged:
            self._index.update(watch.task_id, now=self._now(), state=CLOSED)
            logger.info(
                "PR #%s %s — no longer watching %s",
                watch.pr_number,
                "merged" if pr.merged else "closed",
                watch.task_id,
            )
            return False

        # -- signals --------------------------------------------------------
        comments = _relevant_comments(
            self._github.list_comments(
                repo, watch.pr_number, since=watch.last_comment_at
            )
        )
        watermark = comments[-1].created_at if comments else watch.last_comment_at
        if any(SKIP_MARKER in c.body for c in comments):
            self._index.update(
                watch.task_id, now=self._now(), state=CLOSED,
                last_comment_at=watermark,
            )
            logger.info("PR #%s opted out via '%s'", watch.pr_number, SKIP_MARKER)
            return False

        checks = self._github.check_summary(repo, pr.head_sha)
        ci_failed = (
            checks.conclusion == "failure" and pr.head_sha != watch.last_ci_sha
        )
        has_comments = bool(comments)

        if not (ci_failed or has_comments):
            if watermark != watch.last_comment_at:
                self._index.update(
                    watch.task_id, now=self._now(), last_comment_at=watermark
                )
            return False

        # -- signal present: fix (if under cap) or escalate -----------------
        if watch.fix_rounds >= MAX_FIX_ROUNDS:
            reason = (
                f"PR #{watch.pr_number} still needs work after {MAX_FIX_ROUNDS} "
                f"automatic fix round(s) — handing to you."
            )
            self._index.update(
                watch.task_id, now=self._now(), state=ESCALATED,
                last_ci_sha=pr.head_sha, last_comment_at=watermark,
            )
            self._safe_comment(repo, watch.pr_number, f"{STROMBOLI_MARKER} {reason}")
            self._on_escalate(watch, reason)
            return True

        ci_logs = self._github.failing_logs(repo, pr.head_sha) if ci_failed else ""
        feedback = _fix_feedback(ci_logs, checks.failing, comments)
        self._index.update(watch.task_id, now=self._now(), state=FIXING)
        self._run_fix(watch, repo, feedback, pr.head_sha, watermark)
        return True

    def _run_fix(
        self,
        watch: PRWatch,
        repo: Repo,
        feedback: str,
        head_sha: str,
        watermark: str | None,
    ) -> None:
        """One bounded fix cycle: resume coder → sandbox → verify → push."""
        worktree = self._wm.ensure_from_branch(
            repo, watch.task_id, watch.goal, watch.branch
        )
        coding = make_coding(self._coder, self._sandbox, lambda _s: worktree)
        verify = make_verifier(self._gateway, model=self._verifier_model)

        state = StromboliState(
            task_id=watch.task_id,
            source="notion",
            raw_request=watch.goal,
            spec=Spec(goal=watch.goal),
            plan=watch.goal,
            session_id=watch.session_id,
            # A revise verdict is exactly how the coding node injects feedback.
            verdict=Verdict(decision="revise", reason=feedback),
        )
        state = _merge(state, coding(state))
        if state.status == "escalated":  # rate-limit cutoff mid-fix
            self._index.update(
                watch.task_id, now=self._now(), state=ESCALATED,
                session_id=state.session_id, last_ci_sha=head_sha,
                last_comment_at=watermark,
            )
            self._on_escalate(watch, "rate-limited mid-fix; resume after the window")
            return

        state = _merge(state, verify(state))
        rounds = watch.fix_rounds + 1
        if state.verdict is None or state.verdict.decision != "pass":
            note = state.verdict.reason if state.verdict else "verifier failed"
            self._index.update(
                watch.task_id, now=self._now(), state=ESCALATED,
                session_id=state.session_id, fix_rounds=rounds,
                last_ci_sha=head_sha, last_comment_at=watermark,
            )
            self._safe_comment(
                repo, watch.pr_number,
                f"{STROMBOLI_MARKER} fix round {rounds} didn't pass verification "
                f"({note}) — handing to you.",
            )
            self._on_escalate(watch, f"fix round {rounds} rejected: {note}")
            return

        commit_and_push(worktree)
        self._index.update(
            watch.task_id, now=self._now(), state=WATCHING,
            session_id=state.session_id, fix_rounds=rounds,
            last_ci_sha=head_sha, last_comment_at=watermark,
        )
        self._safe_comment(
            repo, watch.pr_number,
            f"{STROMBOLI_MARKER} pushed a fix (round {rounds}) addressing the "
            f"feedback; re-running checks.",
        )
        logger.info("PR #%s fix round %s pushed for %s",
                    watch.pr_number, rounds, watch.task_id)

    def _safe_comment(self, repo: Repo, number: int, body: str) -> None:
        try:
            self._github.add_comment(repo, number, body)
        except Exception as exc:  # noqa: BLE001 - a comment must never crash a sweep
            logger.warning("PR comment failed for #%s: %s", number, exc)


def _merge(state: StromboliState, partial: dict[str, Any]) -> StromboliState:
    """Apply a node's partial-state output, honouring the append reducers."""
    data = state.model_dump()
    for key, value in partial.items():
        if key in ("test_results", "reflections"):
            data[key] = list(data.get(key) or []) + list(value)
        else:
            data[key] = value
    return StromboliState.model_validate(data)


#: A fix left in ``fixing`` longer than this (a crashed sweep) is recoverable.
STALE_FIXING_SECONDS = 1800


def build_from_settings(settings: Any) -> PRFeedbackService:
    """Wire the production PR feedback service from env-backed settings."""
    from datetime import UTC, datetime, timedelta

    from stromboli.graph import _deps_from_settings, _open_pr_index
    from stromboli.integrations.notion import STATUS_REVIEW
    from stromboli.sandbox.runner import WorktreeManager

    deps = _deps_from_settings(settings)
    index = _open_pr_index(settings.workspace_root)
    wm = WorktreeManager(settings.workspace_root, token=settings.github_token)

    def on_escalate(watch: PRWatch, reason: str) -> None:
        # The PR already carries a Stromboli comment; flag it for the human on
        # the board + Telegram. (Merging is human, so Review is the right park.)
        deps.notifier.escalation(watch.task_id, f"PR #{watch.pr_number}: {reason}")
        if deps.notion is not None:
            try:
                deps.notion.update_task(watch.task_id, status=STATUS_REVIEW)
            except Exception:  # noqa: BLE001 - status write must not crash
                logger.warning("Notion Review write failed for %s", watch.task_id)

    def is_stale(updated_at: str) -> bool:
        if not updated_at:
            return True
        try:
            when = datetime.fromisoformat(updated_at)
        except ValueError:
            return True
        return datetime.now(UTC) - when > timedelta(seconds=STALE_FIXING_SECONDS)

    return PRFeedbackService(
        index=index,
        github=deps.github,
        coder=deps.coder,
        sandbox=deps.sandbox,
        gateway=deps.gateway,
        verifier_model=deps.verifier_model,
        worktree_manager=wm,
        now=lambda: datetime.now(UTC).isoformat(),
        on_escalate=on_escalate,
        stale_fixing_cutoff=is_stale,
    )


__all__ = [
    "MAX_FIX_ROUNDS",
    "SKIP_MARKER",
    "STALE_FIXING_SECONDS",
    "STROMBOLI_MARKER",
    "PRFeedbackService",
    "build_from_settings",
]
