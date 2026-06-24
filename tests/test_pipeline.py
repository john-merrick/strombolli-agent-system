"""Tests for the end-to-end build pipeline composition.

This wires the per-story pieces — worktree (US-005), PRD compile (US-006), Ralph
loop (US-007), circuit breaker (US-008), PR (US-010), routing (US-011),
write-back (US-012), and observability (US-013) — into a single ``run_build``.
Every collaborator is injected, so the three terminal paths (PR opened →
Complete, breaker trip → Review, empty diff → Review) are asserted without git,
the network, or a real ``claude``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from stromboli.breaker import BreakerConfig, BreakerTrip, TripReason
from stromboli.loop import LoopResult, StopReason
from stromboli.notion import Repo, Task
from stromboli.observability import NullTracer
from stromboli.pipeline import BuildDeps, run_build
from stromboli.pr import PullRequest
from stromboli.prd import PRD_RELATIVE_PATH
from stromboli.worktree import Worktree


def _task(**overrides: Any) -> Task:
    base: dict[str, Any] = {
        "page_id": "page-1",
        "name": "Add healthcheck",
        "project_ids": ("proj-1",),
        "status": "Working on",
        "spec": "- Return 200 on /health\n- Document it",
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
    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self.status: str | None = None
        self.pr_url: str | None = None
        self.appended: list[str] = []

    def get_project_repo(self, task: Task) -> Repo:
        return self._repo

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


class FakeWorktrees:
    """Yields a real on-disk worktree dir (so PRD writes land) under tmp_path."""

    def __init__(self, root: Path, repo: Repo) -> None:
        self._root = root
        self._repo = repo
        self.removed = False

    @contextmanager
    def worktree(
        self, repo: Repo, task_id: str, task_name: str
    ) -> Iterator[Worktree]:
        path = self._root / "wt"
        path.mkdir(parents=True, exist_ok=True)
        wt = Worktree(
            path=path,
            branch=f"stromboli/{task_id}",
            repo=repo,
            clone_path=self._root / "clone",
        )
        try:
            yield wt
        finally:
            self.removed = True


class FakeLoop:
    """A loop stub that writes a finished PRD and returns a scripted result."""

    def __init__(self, *, reason: StopReason, trip: BreakerTrip | None = None) -> None:
        self._reason = reason
        self._trip = trip

    def run(self, worktree_root: str | Path) -> LoopResult:
        prd = Path(worktree_root) / PRD_RELATIVE_PATH
        prd.parent.mkdir(parents=True, exist_ok=True)
        prd.write_text(
            json.dumps(
                {
                    "items": [
                        {"id": "AC-001", "passes": True},
                        {
                            "id": "AC-002",
                            "blocked": True,
                            "blockReason": "needs manual reconciliation",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        return LoopResult(
            reason=self._reason,
            iterations=(),
            prd_path=prd,
            progress_path=Path(worktree_root) / "scripts" / "progress.txt",
            trip=self._trip,
        )


class FakeGit:
    """A stdout-returning git seam: controls has-changes, no-ops push/commit."""

    def __init__(self, *, has_changes: bool) -> None:
        self._ahead = "1" if has_changes else "0"

    def __call__(self, args: Any) -> str:
        if "rev-list" in args:
            return self._ahead
        return ""  # status (clean) / add / commit / push are all no-ops


def _deps(
    tmp_path: Path,
    notion: FakeNotion,
    github: FakeGitHub,
    loop: FakeLoop,
    *,
    has_changes: bool = True,
) -> BuildDeps:
    repo = Repo(owner="o", repo="r")
    return BuildDeps(
        notion=notion,
        github=github,
        worktrees=FakeWorktrees(tmp_path, repo),
        tracer=NullTracer(),
        breaker_config=BreakerConfig(max_iterations=25, max_cost_usd=5.0),
        make_loop=lambda breaker: loop,
        # Inject the git seam so PR mechanics never shell out.
        git_run=FakeGit(has_changes=has_changes),
    )


# --------------------------------------------------------------------------- #
# Terminal paths                                                              #
# --------------------------------------------------------------------------- #


def test_success_opens_pr_routes_complete_and_writes_summary(tmp_path: Path) -> None:
    notion = FakeNotion(Repo(owner="o", repo="r"))
    github = FakeGitHub()
    loop = FakeLoop(reason=StopReason.COMPLETE)

    run_build(_task(needs_review=False), _deps(tmp_path, notion, github, loop))

    assert github.opened == 1
    assert notion.pr_url == "https://github.com/o/r/pull/5"
    assert notion.status == "Complete"
    # The feedback summary was appended and carries the blocked item's reason.
    assert notion.appended
    assert "needs manual reconciliation" in notion.appended[-1]


def test_needs_review_routes_to_review(tmp_path: Path) -> None:
    notion = FakeNotion(Repo(owner="o", repo="r"))
    github = FakeGitHub()
    loop = FakeLoop(reason=StopReason.COMPLETE)

    run_build(_task(needs_review=True), _deps(tmp_path, notion, github, loop))

    assert github.opened == 1
    assert notion.status == "Review"


def test_breaker_trip_routes_to_review_without_opening_a_pr(tmp_path: Path) -> None:
    notion = FakeNotion(Repo(owner="o", repo="r"))
    github = FakeGitHub()
    trip = BreakerTrip(
        reason=TripReason.COST, iterations=6, total_cost_usd=6.0, limit=5.0
    )
    loop = FakeLoop(reason=StopReason.BREAKER, trip=trip)

    run_build(_task(), _deps(tmp_path, notion, github, loop))

    assert github.opened == 0  # no PR on a trip
    assert notion.status == "Review"
    assert any("circuit breaker" in a.lower() for a in notion.appended)


def test_empty_diff_routes_to_review_without_opening_a_pr(tmp_path: Path) -> None:
    notion = FakeNotion(Repo(owner="o", repo="r"))
    github = FakeGitHub()
    loop = FakeLoop(reason=StopReason.COMPLETE)

    run_build(
        _task(needs_review=False),
        _deps(tmp_path, notion, github, loop, has_changes=False),
    )

    assert github.opened == 0  # nothing to ship
    assert notion.status == "Review"
    assert notion.pr_url is None


def test_worktree_is_cleaned_up(tmp_path: Path) -> None:
    notion = FakeNotion(Repo(owner="o", repo="r"))
    github = FakeGitHub()
    loop = FakeLoop(reason=StopReason.COMPLETE)
    deps = _deps(tmp_path, notion, github, loop)

    run_build(_task(), deps)

    # The context manager tore the worktree down (cleanup on the way out).
    assert isinstance(deps.worktrees, FakeWorktrees)
    assert deps.worktrees.removed is True


# --------------------------------------------------------------------------- #
# Engine selection — graph path consumes the same finalize                    #
# --------------------------------------------------------------------------- #
class FakeGraphResult:
    """A minimal BuildResult the graph engine would return."""

    def __init__(self) -> None:
        self.reason = "integrated"
        self.tripped = False
        self.trip = None
        self.artifact_path = Path(".stromboli/state.json")

    def blocked_items(self) -> list[Any]:
        return []

    def completed_count(self) -> int:
        return 2

    def usage_spans(self) -> list[Any]:
        return []


class FakeGraph:
    """A GraphRunner stub: records its call and returns a scripted result."""

    def __init__(self, result: FakeGraphResult) -> None:
        self._result = result
        self.spec: str | None = None

    def run(
        self, worktree_root: Any, *, spec: str, resume: bool = False
    ) -> FakeGraphResult:
        self.spec = spec
        return self._result


def test_graph_engine_selected_by_flag_uses_same_finalize(tmp_path: Path) -> None:
    from stromboli.pipeline import ENGINE_GRAPH

    notion = FakeNotion(Repo(owner="o", repo="r"))
    github = FakeGitHub()
    graph = FakeGraph(FakeGraphResult())
    repo = Repo(owner="o", repo="r")
    deps = BuildDeps(
        notion=notion,
        github=github,
        worktrees=FakeWorktrees(tmp_path, repo),
        tracer=NullTracer(),
        breaker_config=BreakerConfig(max_iterations=25, max_cost_usd=5.0),
        git_run=FakeGit(has_changes=True),
        engine=ENGINE_GRAPH,
        make_graph=lambda d: graph,
    )

    run_build(_task(needs_review=False, spec="- Return 200 on /health"), deps)

    # The graph engine ran (got the task spec) and its result flowed through the
    # shared integrate/finalize path: PR opened, routed Complete, summary written.
    assert graph.spec == "- Return 200 on /health"
    assert github.opened == 1
    assert notion.status == "Complete"
    assert "Completed:** 2" in notion.appended[-1]


def test_default_engine_path_is_ralph(tmp_path: Path) -> None:
    # With no engine override, the Ralph loop factory is used (a graph factory
    # that explodes proves the default never touches it).
    notion = FakeNotion(Repo(owner="o", repo="r"))
    github = FakeGitHub()
    loop = FakeLoop(reason=StopReason.COMPLETE)
    repo = Repo(owner="o", repo="r")

    def _boom(_: Any) -> Any:
        raise AssertionError("graph factory must not be called for the Ralph default")

    deps = BuildDeps(
        notion=notion,
        github=github,
        worktrees=FakeWorktrees(tmp_path, repo),
        tracer=NullTracer(),
        breaker_config=BreakerConfig(max_iterations=25, max_cost_usd=5.0),
        make_loop=lambda breaker: loop,
        git_run=FakeGit(has_changes=True),
        make_graph=_boom,
    )

    run_build(_task(), deps)
    assert notion.status == "Complete"
