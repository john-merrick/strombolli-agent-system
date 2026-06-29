"""Human Interrupt node (PRD §6.8) — pause for human input on ambiguity/stuck.

Real behavior (Phase 1): call LangGraph ``interrupt()`` to pause the graph
(persisting via the checkpointer), surfacing the question/escalation to Telegram
+ Notion; the graph resumes on edited state. Reached from the Router (ambiguous
spec) or the Verdict gate (escalate / budget exhausted).

Phase 0 stub: mark the task ``escalated`` and terminate (no real interrupt yet).
"""

from __future__ import annotations

import logging

from stromboli.nodes.intake import Node
from stromboli.state import StromboliState

logger = logging.getLogger(__name__)


def make_human() -> Node:
    """Build the human-interrupt node (Phase 0 stub; interrupt() lands in Phase 1)."""

    def human(state: StromboliState) -> dict[str, object]:
        logger.info("Escalating task %s for human attention.", state.task_id)
        return {"status": "escalated"}

    return human


__all__ = ["make_human"]
