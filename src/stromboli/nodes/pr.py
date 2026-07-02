"""PR / Commit node (PRD §6.7) — turn the verified diff into a pull request.

Reached only behind the verifier gate (a ``pass``), so the irreversible action
happens after independent judgment. It commits + pushes the worktree branch and
opens a PR via the GitHub API, writing the URL back. CI is the external gate.

In ``dry_run`` mode it opens no PR but logs the intended action (PRD §6.7 DoD);
with no github/worktree wired it is a stub. Live mode (Phase 6) calls
:func:`~stromboli.integrations.github.publish_pr`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from stromboli.integrations.github import GitHubGateway, GitRunner, publish_pr
from stromboli.integrations.notion import Task
from stromboli.nodes.coding import WorktreeFor
from stromboli.nodes.intake import Node
from stromboli.sandbox.runner import Worktree
from stromboli.state import StromboliState

logger = logging.getLogger(__name__)

#: Called after a live PR is opened, to register it for the feedback loop:
#: ``(state, worktree, pr_url, pr_number)``. ``None`` → not registered.
OnPublished = Callable[[StromboliState, Worktree, str, int], None]


def _task_for_pr(state: StromboliState) -> Task:
    """A minimal :class:`Task` carrying the title/body source for the PR."""
    spec = state.spec
    name = (spec.goal if spec else state.raw_request) or "Stromboli build"
    body = spec.goal if spec else state.raw_request
    if spec and spec.acceptance_criteria:
        body += "\n\nAcceptance criteria:\n" + "\n".join(
            f"- {c}" for c in spec.acceptance_criteria
        )
    return Task(
        page_id=state.task_id,
        name=name,
        project_ids=(),
        status=None,
        spec=body,
        assigned_to=None,
        ready=True,
        needs_review=False,
        pr_url=None,
        cost=None,
        tokens=None,
    )


def make_pr(
    github: GitHubGateway | None = None,
    notion: object | None = None,
    worktree_for: WorktreeFor | None = None,
    *,
    base: str = "main",
    dry_run: bool = True,
    git_run: GitRunner | None = None,
    on_published: OnPublished | None = None,
) -> Node:
    """Build the PR node. ``dry_run`` (default) opens no PR; live needs github."""

    def pr(state: StromboliState) -> dict[str, object]:
        if dry_run or github is None or worktree_for is None:
            logger.info("[dry-run] would open a PR for task %s", state.task_id)
            return {"pr_url": None, "status": "pr"}

        # Only a Notion-sourced task has a page for the PR-URL write-back.
        pr_notion = notion if state.source == "notion" else None
        try:
            worktree = worktree_for(state)
            result = publish_pr(
                pr_notion, github, worktree, _task_for_pr(state),
                base=base, run=git_run,
            )
        except Exception as exc:  # noqa: BLE001 - the last node must not crash
            # The verified diff exists; a push/API failure here is operational
            # (auth, network, branch protection), not a build failure — park it
            # for a human rather than crash and lose the whole run.
            logger.exception("PR publication failed for task %s", state.task_id)
            return {
                "status": "escalated",
                "reflections": [f"PR publication failed: {exc}"],
            }
        if result.empty_diff:
            logger.info("No changes for task %s; routing to review.", state.task_id)
        # Register the opened PR for the feedback loop (best-effort).
        if (
            on_published is not None
            and result.pr_url is not None
            and result.pr_number is not None
        ):
            try:
                on_published(state, worktree, result.pr_url, result.pr_number)
            except Exception as exc:  # noqa: BLE001 - registration must not crash
                logger.warning("PR watch registration failed for %s: %s",
                               state.task_id, exc)
        return {"pr_url": result.pr_url, "status": "pr"}

    return pr


__all__ = ["OnPublished", "make_pr"]
