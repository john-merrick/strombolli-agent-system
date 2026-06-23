"""Integration routing — Review vs Complete (US-011).

After the PR is open the worker decides the task's final status per the autonomy
table:

* ``Needs review == true``  → ``Review``   — a human approves/merges.
* ``Needs review == false`` → ``Complete``  — v1 still leaves the merge to a
  human (no auto-merge), but the worker's job is done.
* ``Assigned to == Human``  → **never** picked up or modified.

A build that produced no changes (no PR) also routes to ``Review`` so a human
can see why nothing shipped.

:func:`decide_route` is a pure function over the task and the publish result, so
every branch of the table is unit-testable; :func:`route_task` applies the
decision to Notion and is a no-op for a Human-assigned task.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Protocol

from stromboli.notion import STATUS_COMPLETE, STATUS_REVIEW, Task
from stromboli.pr import PublishResult
from stromboli.worker import ASSIGNEE_HUMAN

logger = logging.getLogger(__name__)


class RouteOutcome(StrEnum):
    """The routing decision for a finished build."""

    #: Park for a human to approve/merge.
    REVIEW = "review"
    #: Mark done (human still performs the merge in v1).
    COMPLETE = "complete"
    #: Human-assigned task — left untouched.
    SKIPPED_HUMAN = "skipped_human"


class TaskGateway(Protocol):
    """The slice of the Notion client routing needs."""

    def update_task(self, page_id: str, *, status: str | None = ...) -> None: ...


def decide_route(task: Task, publish: PublishResult) -> RouteOutcome:
    """Decide the final route from the task and the publish result.

    A Human-assigned task is never touched. Otherwise an empty diff or a task
    flagged ``Needs review`` goes to ``Review``; a clean, unflagged build with a
    PR goes to ``Complete``.
    """
    if task.assigned_to == ASSIGNEE_HUMAN:
        return RouteOutcome.SKIPPED_HUMAN
    if publish.empty_diff:
        return RouteOutcome.REVIEW
    if task.needs_review:
        return RouteOutcome.REVIEW
    return RouteOutcome.COMPLETE


def route_task(
    notion: TaskGateway, task: Task, publish: PublishResult
) -> RouteOutcome:
    """Apply :func:`decide_route` to Notion; never modify a Human task."""
    outcome = decide_route(task, publish)
    if outcome is RouteOutcome.SKIPPED_HUMAN:
        logger.info("Task %s is Human-assigned; leaving it untouched.", task.page_id)
        return outcome

    status = STATUS_REVIEW if outcome is RouteOutcome.REVIEW else STATUS_COMPLETE
    notion.update_task(task.page_id, status=status)
    logger.info("Routed task %s to %s.", task.page_id, status)
    return outcome


__all__ = [
    "RouteOutcome",
    "TaskGateway",
    "decide_route",
    "route_task",
]
