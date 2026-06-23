"""Tests for integration routing (US-011).

Once the PR is open the worker routes the task per the autonomy table:

* ``Needs review == true``  → ``Review``   (await a human approve/merge)
* ``Needs review == false`` → ``Complete``  (v1: the human still performs the merge)
* ``Assigned to == Human``  → never picked up or modified

An empty-diff build (no PR opened) also routes to ``Review``. The decision is a
pure function so every branch of the table is asserted directly; ``route_task``
applies it to Notion (and leaves a Human task untouched).
"""

from __future__ import annotations

from typing import Any

from stromboli.notion import STATUS_COMPLETE, STATUS_REVIEW, Task
from stromboli.pr import PublishResult
from stromboli.routing import RouteOutcome, decide_route, route_task
from stromboli.worker import ASSIGNEE_AGENT, ASSIGNEE_HUMAN


def _task(**overrides: Any) -> Task:
    base: dict[str, Any] = {
        "page_id": "page-1",
        "name": "T",
        "project_ids": ("p",),
        "status": "Working on",
        "spec": "s",
        "assigned_to": ASSIGNEE_AGENT,
        "ready": True,
        "needs_review": False,
        "pr_url": None,
        "cost": None,
        "tokens": None,
    }
    base.update(overrides)
    return Task(**base)


def _opened() -> PublishResult:
    return PublishResult(pr_url="https://github.com/o/r/pull/1", empty_diff=False)


def _empty() -> PublishResult:
    return PublishResult(pr_url=None, empty_diff=True)


class FakeNotion:
    def __init__(self) -> None:
        self.status: str | None = None
        self.writes = 0

    def update_task(self, page_id: str, *, status: str | None = None, **_: Any) -> None:
        self.writes += 1
        if status is not None:
            self.status = status


# --------------------------------------------------------------------------- #
# decide_route — the autonomy table                                           #
# --------------------------------------------------------------------------- #


def test_needs_review_true_routes_to_review() -> None:
    assert decide_route(_task(needs_review=True), _opened()) is RouteOutcome.REVIEW


def test_needs_review_false_routes_to_complete() -> None:
    assert decide_route(_task(needs_review=False), _opened()) is RouteOutcome.COMPLETE


def test_human_task_is_skipped() -> None:
    assert (
        decide_route(_task(assigned_to=ASSIGNEE_HUMAN), _opened())
        is RouteOutcome.SKIPPED_HUMAN
    )


def test_empty_diff_routes_to_review_even_when_not_needs_review() -> None:
    assert decide_route(_task(needs_review=False), _empty()) is RouteOutcome.REVIEW


# --------------------------------------------------------------------------- #
# route_task — applying the decision                                          #
# --------------------------------------------------------------------------- #


def test_route_task_sets_review() -> None:
    notion = FakeNotion()
    outcome = route_task(notion, _task(needs_review=True), _opened())
    assert outcome is RouteOutcome.REVIEW
    assert notion.status == STATUS_REVIEW


def test_route_task_sets_complete() -> None:
    notion = FakeNotion()
    outcome = route_task(notion, _task(needs_review=False), _opened())
    assert outcome is RouteOutcome.COMPLETE
    assert notion.status == STATUS_COMPLETE


def test_route_task_never_modifies_a_human_task() -> None:
    notion = FakeNotion()
    outcome = route_task(notion, _task(assigned_to=ASSIGNEE_HUMAN), _opened())
    assert outcome is RouteOutcome.SKIPPED_HUMAN
    assert notion.writes == 0  # the task is left completely untouched
    assert notion.status is None
