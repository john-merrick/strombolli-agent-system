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

from stromboli.integrations.github import GitHubGateway, GitRunner, publish_pr
from stromboli.integrations.notion import Task
from stromboli.nodes.coding import WorktreeFor
from stromboli.nodes.intake import Node
from stromboli.state import StromboliState

logger = logging.getLogger(__name__)


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
) -> Node:
    """Build the PR node. ``dry_run`` (default) opens no PR; live needs github."""

    def pr(state: StromboliState) -> dict[str, object]:
        if dry_run or github is None or worktree_for is None:
            logger.info("[dry-run] would open a PR for task %s", state.task_id)
            return {"pr_url": None, "status": "pr"}

        worktree = worktree_for(state)
        result = publish_pr(
            notion, github, worktree, _task_for_pr(state), base=base, run=git_run
        )
        if result.empty_diff:
            logger.info("No changes for task %s; routing to review.", state.task_id)
        return {"pr_url": result.pr_url, "status": "pr"}

    return pr


__all__ = ["make_pr"]
