"""Tests for :mod:`stromboli.worker` — the dispatch guard, idempotent claim,
and serial build lock (US-004).

These assert the *business* contract that sits in front of the build pipeline:

* The guard builds **only** when the task is Ready, Assigned to the Agent, and
  still in the ``To do`` state; every other case logs a reason and exits
  without building.
* A valid claim writes ``Status=Working on`` **before** the build runs.
* A task already in ``Working on`` / ``Review`` / ``Complete`` is a no-op, so a
  duplicate dispatch does nothing (idempotency).
* A single in-process lock serialises builds: a dispatch arriving while a build
  is running is rejected rather than run concurrently.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

from stromboli.notion import (
    STATUS_COMPLETE,
    STATUS_REVIEW,
    STATUS_TODO,
    STATUS_WORKING,
    Task,
)
from stromboli.worker import ASSIGNEE_AGENT, DispatchOutcome, Worker

PAGE_ID = "page-123"


def _task(**overrides: Any) -> Task:
    """A task that passes the guard by default; override fields per test."""
    base: dict[str, Any] = {
        "page_id": PAGE_ID,
        "name": "Build a thing",
        "project_ids": ("proj-1",),
        "status": STATUS_TODO,
        "spec": "do the thing",
        "assigned_to": ASSIGNEE_AGENT,
        "ready": True,
        "needs_review": False,
        "pr_url": None,
        "cost": None,
        "tokens": None,
    }
    base.update(overrides)
    return Task(**base)


class FakeNotion:
    """A minimal stand-in for :class:`NotionTaskClient`.

    Records an ordered event log (shared with the build fn) and reflects status
    writes back into the stored task so a re-dispatch observes the new status.
    """

    def __init__(self, task: Task, events: list[Any] | None = None) -> None:
        self._task = task
        self.events: list[Any] = events if events is not None else []

    def get_task(self, page_id: str) -> Task:
        return self._task

    def update_task(self, page_id: str, *, status: str | None = None, **_: Any) -> None:
        if status is not None:
            self.events.append(("status", status))
            self._task = replace(self._task, status=status)


def test_guard_pass_claims_then_builds() -> None:
    events: list[Any] = []
    notion = FakeNotion(_task(), events)

    def build(task: Task) -> None:
        events.append(("build", task.page_id))

    outcome = Worker(notion, build=build).dispatch(PAGE_ID)

    assert outcome is DispatchOutcome.CLAIMED
    # Status must flip to "Working on" BEFORE the build runs.
    assert events == [("status", STATUS_WORKING), ("build", PAGE_ID)]


def test_not_ready_skips_without_building() -> None:
    events: list[Any] = []
    notion = FakeNotion(_task(ready=False), events)

    outcome = Worker(notion, build=lambda t: events.append("build")).dispatch(PAGE_ID)

    assert outcome is DispatchOutcome.NOT_READY
    assert events == []  # no claim write, no build


def test_not_assigned_to_agent_skips_without_building() -> None:
    events: list[Any] = []
    notion = FakeNotion(_task(assigned_to="Human"), events)

    outcome = Worker(notion, build=lambda t: events.append("build")).dispatch(PAGE_ID)

    assert outcome is DispatchOutcome.NOT_AGENT
    assert events == []


def test_unassigned_skips_without_building() -> None:
    events: list[Any] = []
    notion = FakeNotion(_task(assigned_to=None), events)

    outcome = Worker(notion, build=lambda t: events.append("build")).dispatch(PAGE_ID)

    assert outcome is DispatchOutcome.NOT_AGENT
    assert events == []


def test_unexpected_status_skips_without_building() -> None:
    events: list[Any] = []
    notion = FakeNotion(_task(status=None), events)

    outcome = Worker(notion, build=lambda t: events.append("build")).dispatch(PAGE_ID)

    assert outcome is DispatchOutcome.WRONG_STATUS
    assert events == []


def test_already_working_on_is_a_noop() -> None:
    events: list[Any] = []
    notion = FakeNotion(_task(status=STATUS_WORKING), events)

    outcome = Worker(notion, build=lambda t: events.append("build")).dispatch(PAGE_ID)

    assert outcome is DispatchOutcome.ALREADY_CLAIMED
    assert events == []  # no re-claim, no build


def test_already_review_is_a_noop() -> None:
    notion = FakeNotion(_task(status=STATUS_REVIEW))
    built: list[str] = []

    outcome = Worker(notion, build=lambda t: built.append(t.page_id)).dispatch(PAGE_ID)

    assert outcome is DispatchOutcome.ALREADY_CLAIMED
    assert built == []


def test_already_complete_is_a_noop() -> None:
    notion = FakeNotion(_task(status=STATUS_COMPLETE))
    built: list[str] = []

    outcome = Worker(notion, build=lambda t: built.append(t.page_id)).dispatch(PAGE_ID)

    assert outcome is DispatchOutcome.ALREADY_CLAIMED
    assert built == []


def test_double_dispatch_is_idempotent() -> None:
    events: list[Any] = []
    notion = FakeNotion(_task(), events)

    def build(task: Task) -> None:
        events.append(("build", task.page_id))

    worker = Worker(notion, build=build)

    first = worker.dispatch(PAGE_ID)
    second = worker.dispatch(PAGE_ID)

    assert first is DispatchOutcome.CLAIMED
    assert second is DispatchOutcome.ALREADY_CLAIMED
    # Claimed + built exactly once across both dispatches.
    assert events == [("status", STATUS_WORKING), ("build", PAGE_ID)]


def test_concurrent_dispatch_is_rejected_while_a_build_runs() -> None:
    started = threading.Event()
    release = threading.Event()

    def build(task: Task) -> None:
        started.set()
        assert release.wait(timeout=2), "build was never released"

    notion = FakeNotion(_task())
    worker = Worker(notion, build=build)
    results: dict[str, DispatchOutcome] = {}

    first = threading.Thread(
        target=lambda: results.__setitem__("first", worker.dispatch(PAGE_ID))
    )
    first.start()
    try:
        assert started.wait(timeout=2), "first build never started"
        # The lock is held by the in-flight build, so a concurrent dispatch is
        # rejected rather than run in parallel.
        assert worker.dispatch(PAGE_ID) is DispatchOutcome.BUSY
    finally:
        release.set()
        first.join(timeout=2)

    assert results["first"] is DispatchOutcome.CLAIMED


def test_dispatch_releases_the_lock_after_a_build() -> None:
    # A second, sequential dispatch must succeed: the lock is released so the
    # worker is not wedged after one build.
    notion = FakeNotion(_task())
    worker = Worker(notion, build=lambda t: None)

    assert worker.dispatch(PAGE_ID) is DispatchOutcome.CLAIMED
    # Task is now "Working on", so the second sequential dispatch is a no-op,
    # which still proves the lock was released (we got an outcome, not BUSY).
    assert worker.dispatch(PAGE_ID) is DispatchOutcome.ALREADY_CLAIMED
