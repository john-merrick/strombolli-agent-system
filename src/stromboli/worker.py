"""The dispatch guard, idempotent claim, and serial build lock (US-004).

US-003's HTTP layer only authenticates the request (the shared secret). The
*business* decision of whether a given task should be built — and the guarantee
that only one build runs at a time — lives here, in front of the (future) build
pipeline.

The guard proceeds only when the task is **Ready**, **Assigned to the Agent**,
and still in the **``To do``** state. On a valid claim it writes
``Status=Working on`` *before* the build runs, so a duplicate dispatch observes
the claimed status and becomes a no-op (idempotency). A single in-process lock
serialises builds: a dispatch arriving while a build is already running is
rejected with a logged reason rather than run concurrently.

The build itself (worktree, Ralph loop, PR, write-back) is injected as
``build`` so this gate is independently unit-testable; later stories pass the
real pipeline.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from enum import StrEnum
from typing import Final, Protocol

from stromboli.notion import (
    STATUS_COMPLETE,
    STATUS_REVIEW,
    STATUS_TODO,
    STATUS_WORKING,
    Task,
)

logger = logging.getLogger(__name__)

#: The ``Assigned to`` value that marks a task as the agent's to build.
ASSIGNEE_AGENT: Final = "Agent"

#: Statuses meaning the task has already been claimed — re-dispatch is a no-op.
_CLAIMED_STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_WORKING, STATUS_REVIEW, STATUS_COMPLETE}
)

#: Build entrypoint: given the claimed task, run the build pipeline.
BuildFn = Callable[[Task], None]


class TaskGateway(Protocol):
    """The subset of :class:`~stromboli.notion.NotionTaskClient` the gate needs.

    A structural type so the worker depends on behaviour, not a concrete class —
    :class:`NotionTaskClient` satisfies it, and tests inject a lightweight fake.
    """

    def get_task(self, page_id: str) -> Task: ...

    def update_task(self, page_id: str, *, status: str | None = ...) -> None: ...


class DispatchOutcome(StrEnum):
    """The result of a dispatch — exactly one branch of the guard / lock."""

    #: Guard passed: the task was claimed (Status->Working on) and built.
    CLAIMED = "claimed"
    #: ``Ready`` is not checked.
    NOT_READY = "not_ready"
    #: ``Assigned to`` is not the Agent.
    NOT_AGENT = "not_assigned_to_agent"
    #: Ready + Agent but Status is neither ``To do`` nor a claimed status.
    WRONG_STATUS = "wrong_status"
    #: Already Working on / Review / Complete — idempotent no-op.
    ALREADY_CLAIMED = "already_claimed"
    #: Another build holds the serial lock; this dispatch was rejected.
    BUSY = "busy"

    @property
    def built(self) -> bool:
        """Whether this outcome ran the build."""
        return self is DispatchOutcome.CLAIMED


def _noop_build(task: Task) -> None:  # pragma: no cover - placeholder
    """Default build used until the real pipeline is wired in (US-005+)."""


def evaluate_guard(task: Task) -> DispatchOutcome:
    """Decide what to do with ``task`` based purely on its fields.

    A claimed status (Working on / Review / Complete) short-circuits to a no-op
    so a duplicate dispatch never re-claims or rebuilds. Otherwise the task must
    be Ready, Assigned to the Agent, and in ``To do`` to be claimed.
    """
    if task.status in _CLAIMED_STATUSES:
        return DispatchOutcome.ALREADY_CLAIMED
    if not task.ready:
        return DispatchOutcome.NOT_READY
    if task.assigned_to != ASSIGNEE_AGENT:
        return DispatchOutcome.NOT_AGENT
    if task.status != STATUS_TODO:
        return DispatchOutcome.WRONG_STATUS
    return DispatchOutcome.CLAIMED


class Worker:
    """Serial dispatch gate in front of the build pipeline."""

    def __init__(self, notion: TaskGateway, *, build: BuildFn | None = None) -> None:
        self._notion = notion
        self._build = build or _noop_build
        # Non-reentrant lock enforcing one build at a time across threads.
        self._lock = threading.Lock()

    def dispatch(self, page_id: str) -> DispatchOutcome:
        """Guard, claim, and (if eligible) build ``page_id``; never concurrent.

        Returns the :class:`DispatchOutcome` describing what happened. A return
        value other than :attr:`DispatchOutcome.CLAIMED` means no build ran.
        """
        # Reject (don't block) when a build is already in flight — a serial
        # worker, so a concurrent dispatch is turned away with a logged reason.
        if not self._lock.acquire(blocking=False):
            logger.info(
                "Dispatch for %s rejected: a build is already in progress.", page_id
            )
            return DispatchOutcome.BUSY
        try:
            return self._claim_and_build(page_id)
        finally:
            self._lock.release()

    def _claim_and_build(self, page_id: str) -> DispatchOutcome:
        task = self._notion.get_task(page_id)
        outcome = evaluate_guard(task)
        if outcome is not DispatchOutcome.CLAIMED:
            logger.info("Dispatch for %s skipped: %s.", page_id, outcome.value)
            return outcome

        # Claim the task before building so a duplicate dispatch sees
        # "Working on" and short-circuits to a no-op.
        self._notion.update_task(page_id, status=STATUS_WORKING)
        logger.info("Claimed %s; starting build.", page_id)
        self._build(task)
        return DispatchOutcome.CLAIMED


__all__ = [
    "ASSIGNEE_AGENT",
    "BuildFn",
    "DispatchOutcome",
    "Worker",
    "evaluate_guard",
]
