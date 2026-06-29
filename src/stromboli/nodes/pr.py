"""PR / Commit node (PRD §6.7) — turn the verified diff into a pull request.

Real behavior (Phase 1 dry-run → Phase 6 live): commit + push the worktree
branch and open a PR via the GitHub API, writing the URL back to Notion. In
``dry_run`` mode it opens no PR but logs the intended action (PRD §6.7 DoD).

Phase 0 stub: ``dry_run`` only — record a placeholder URL and advance status.
"""

from __future__ import annotations

import logging

from stromboli.nodes.intake import Node
from stromboli.state import StromboliState

logger = logging.getLogger(__name__)


def make_pr(*, dry_run: bool = True) -> Node:
    """Build the PR node. ``dry_run`` opens no PR (Phase 0/1 default)."""

    def pr(state: StromboliState) -> dict[str, object]:
        if dry_run:
            logger.info("[dry-run] would open a PR for task %s", state.task_id)
            return {"pr_url": None, "status": "pr"}
        # Live PR path is wired in Phase 6 (github + notion write-back).
        return {"pr_url": None, "status": "pr"}

    return pr


__all__ = ["make_pr"]
