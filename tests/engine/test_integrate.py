"""Tests for the engine integrator adapter (Phase 3).

The adapter is the single finalize path both engines share. These assert the
three terminal routes (trip → Review, PR opened → write-back, empty diff →
Review) and the finalize side-effects (feedback append + trace), driving the
already-covered building blocks through fakes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stromboli.breaker import BreakerTrip, TripReason
from stromboli.engine.integrate import finalize_build, integrate_build
from stromboli.engine.result import UsageSpan
from stromboli.notion import Repo, Task
from stromboli.observability import BuildTracer
from stromboli.pr import PullRequest
from stromboli.worktree import Worktree


def _task(**overrides: Any) -> Task:
    base: dict[str, Any] = {
        "page_id": "page-1",
        "name": "Add healthcheck",
        "project_ids": ("proj-1",),
        "status": "Working on",
        "spec": "- Return 200 on /health",
        "assigned_to": "Agent",
        "ready": True,
        "needs_review": False,
        "pr_url": None,
        "cost": None,
        "tokens": None,
    }
    base.update(overrides)
    return Task(**base)


class FakeNotion:
    def __init__(self) -> None:
        self.status: str | None = None
        self.pr_url: str | None = None
        self.appended: list[str] = []

    def update_task(
        self, page_id: str, *, status: str | None = None, pr_url: Any = None, **_: Any
    ) -> None:
        if status is not None:
            self.status = status
        if pr_url is not None:
            self.pr_url = pr_url

    def append_task_body(self, page_id: str, markdown: str) -> None:
        self.appended.append(markdown)


class FakeGitHub:
    def __init__(self) -> None:
        self.opened = 0

    def open_pull_request(
        self, repo: Any, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        self.opened += 1
        return PullRequest(url="https://github.com/o/r/pull/5", number=5)


class FakeResult:
    """A minimal BuildResult for the adapter."""

    def __init__(
        self,
        *,
        tripped: bool = False,
        trip: BreakerTrip | None = None,
        blocked: list[Any] | None = None,
        completed: int = 1,
    ) -> None:
        self.reason = "done"
        self.tripped = tripped
        self.trip = trip
        self.artifact_path = Path("scripts/prd.json")
        self._blocked = blocked or []
        self._completed = completed

    def blocked_items(self) -> list[Any]:
        return self._blocked

    def completed_count(self) -> int:
        return self._completed

    def usage_spans(self) -> list[UsageSpan]:
        return [UsageSpan(index=1, cost_usd=0.1, input_tokens=5, output_tokens=2)]


class RecordingTracer(BuildTracer):
    def __init__(self) -> None:
        self.started = False
        self.finished = False

    def start(self, *, task_id: str, name: str) -> None:
        self.started = True

    def finish(self, *, error: str | None = None) -> None:
        self.finished = True


def _worktree(tmp_path: Path) -> Worktree:
    return Worktree(
        path=tmp_path,
        branch="stromboli/page-1",
        repo=Repo(owner="o", repo="r"),
        clone_path=tmp_path,
    )


def _git_changes(*_: Any) -> str:
    """Fake git runner: report a non-empty diff so a PR is opened."""
    return "M app.py"


def _git_empty(*_: Any) -> str:
    return ""


# --------------------------------------------------------------------------- #
# integrate_build — the three routes                                          #
# --------------------------------------------------------------------------- #
def test_trip_routes_to_review_without_pr(tmp_path: Path) -> None:
    notion, github = FakeNotion(), FakeGitHub()
    trip = BreakerTrip(
        reason=TripReason.COST, iterations=3, total_cost_usd=11.0, limit=10.0
    )
    pr_url = integrate_build(
        task=_task(),
        worktree=_worktree(tmp_path),
        result=FakeResult(tripped=True, trip=trip),
        notion=notion,
        github=github,
    )
    assert pr_url is None
    assert github.opened == 0
    assert notion.status == "Review"


def test_pr_opened_and_routed_complete(tmp_path: Path) -> None:
    notion, github = FakeNotion(), FakeGitHub()
    pr_url = integrate_build(
        task=_task(),
        worktree=_worktree(tmp_path),
        result=FakeResult(),
        notion=notion,
        github=github,
        git_run=_git_changes,
    )
    assert pr_url == "https://github.com/o/r/pull/5"
    assert github.opened == 1
    assert notion.status == "Complete"


def test_empty_diff_routes_to_review(tmp_path: Path) -> None:
    notion, github = FakeNotion(), FakeGitHub()
    pr_url = integrate_build(
        task=_task(),
        worktree=_worktree(tmp_path),
        result=FakeResult(),
        notion=notion,
        github=github,
        git_run=_git_empty,
    )
    assert pr_url is None
    assert github.opened == 0
    assert notion.status == "Review"


# --------------------------------------------------------------------------- #
# finalize_build — feedback append + trace                                    #
# --------------------------------------------------------------------------- #
def test_finalize_appends_summary_and_records_trace() -> None:
    notion = FakeNotion()
    tracer = RecordingTracer()
    finalize_build(
        task=_task(),
        result=FakeResult(completed=2),
        pr_url="https://github.com/o/r/pull/5",
        error=None,
        notion=notion,
        tracer=tracer,
    )
    assert notion.appended, "a feedback summary should be appended"
    assert "Completed:** 2" in notion.appended[0]
    assert tracer.started and tracer.finished


def test_finalize_includes_error_note_and_tolerates_no_result() -> None:
    notion = FakeNotion()
    tracer = RecordingTracer()
    finalize_build(
        task=_task(),
        result=None,
        pr_url=None,
        error="RuntimeError: boom",
        notion=notion,
        tracer=tracer,
    )
    assert "Build error: RuntimeError: boom" in notion.appended[0]
    # No result → no trace recorded.
    assert tracer.started is False
