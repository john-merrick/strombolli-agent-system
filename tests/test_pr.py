"""Tests for opening the PR and writing it back to Notion (US-010).

After a successful loop the worker commits any residual changes, pushes the
branch, opens a PR against ``main`` with a title/body derived from the task and
spec, and writes the PR URL to the task's ``PR`` field. When the branch produced
no changes, no empty PR is opened — the caller routes the task to ``Review``
(US-011/US-012).

Git and the GitHub API are reached through injected seams (a stdout-returning
git runner and an ``httpx`` client), so derivation, the commit/push argv, the
PR call, and the empty-diff branch are all asserted without a network or a real
repository.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from stromboli.notion import Repo
from stromboli.pr import (
    GitHubClient,
    PublishResult,
    branch_has_changes,
    derive_pr_body,
    derive_pr_title,
    publish_pr,
)
from stromboli.worktree import Worktree


def _task(**overrides: Any) -> Any:
    """A lightweight task stand-in exposing the fields PR derivation reads."""
    from stromboli.notion import Task

    base: dict[str, Any] = {
        "page_id": "page-abc",
        "name": "Add a healthcheck endpoint",
        "project_ids": ("proj-1",),
        "status": "Working on",
        "spec": "- Return 200 on /health\n- Document it in the README",
        "assigned_to": "Agent",
        "ready": True,
        "needs_review": False,
        "pr_url": None,
        "cost": None,
        "tokens": None,
    }
    base.update(overrides)
    return Task(**base)


def _worktree(tmp_path: Any) -> Worktree:
    repo = Repo(owner="acme", repo="widgets")
    return Worktree(
        path=tmp_path / "wt",
        branch="stromboli/page-abc-add-a-healthcheck-endpoint",
        repo=repo,
        clone_path=tmp_path / "clone",
    )


class RecordingGit:
    """A stdout-returning git runner that records argv and replays output."""

    def __init__(self, *, status: str = "", ahead: str = "1") -> None:
        self._status = status
        self._ahead = ahead
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        if "status" in args:
            return self._status
        if "rev-list" in args:
            return self._ahead
        return ""

    def ran(self, *needles: str) -> bool:
        """True if some recorded call contains all the given argv tokens."""
        return any(all(n in call for n in needles) for call in self.calls)


class FakeNotion:
    def __init__(self) -> None:
        self.pr_url: str | None = None

    def update_task(self, page_id: str, *, pr_url: Any = None, **_: Any) -> None:
        if pr_url is not None:
            self.pr_url = pr_url


# --------------------------------------------------------------------------- #
# Pure derivation                                                             #
# --------------------------------------------------------------------------- #


def test_pr_title_derives_from_the_task() -> None:
    title = derive_pr_title(_task())
    assert "Add a healthcheck endpoint" in title


def test_pr_body_includes_spec_and_task_reference() -> None:
    body = derive_pr_body(_task())
    assert "Return 200 on /health" in body  # the spec is carried into the body
    assert "page-abc" in body  # traceable back to the originating task


# --------------------------------------------------------------------------- #
# GitHub client                                                               #
# --------------------------------------------------------------------------- #


def test_github_client_opens_pr_and_parses_url() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={"html_url": "https://github.com/acme/widgets/pull/7", "number": 7},
        )

    client = GitHubClient(
        token="ghtok",
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )
    pr = client.open_pull_request(
        Repo(owner="acme", repo="widgets"),
        head="stromboli/x",
        base="main",
        title="T",
        body="B",
    )

    assert pr.url == "https://github.com/acme/widgets/pull/7"
    assert pr.number == 7
    assert "/repos/acme/widgets/pulls" in captured["url"]
    assert captured["body"] == {
        "title": "T",
        "body": "B",
        "head": "stromboli/x",
        "base": "main",
    }


# --------------------------------------------------------------------------- #
# branch_has_changes                                                          #
# --------------------------------------------------------------------------- #


def test_branch_has_changes_true_when_commits_ahead(tmp_path: Any) -> None:
    git = RecordingGit(status="", ahead="2")
    assert branch_has_changes(_worktree(tmp_path), run=git) is True


def test_branch_has_changes_true_when_uncommitted(tmp_path: Any) -> None:
    git = RecordingGit(status=" M src/foo.py", ahead="0")
    assert branch_has_changes(_worktree(tmp_path), run=git) is True


def test_branch_has_changes_false_when_clean_and_no_commits(tmp_path: Any) -> None:
    git = RecordingGit(status="", ahead="0")
    assert branch_has_changes(_worktree(tmp_path), run=git) is False


# --------------------------------------------------------------------------- #
# publish_pr orchestration                                                    #
# --------------------------------------------------------------------------- #


def test_publish_pr_commits_pushes_opens_and_writes_back(tmp_path: Any) -> None:
    git = RecordingGit(status=" M f.py", ahead="1")
    notion = FakeNotion()
    opened: dict[str, Any] = {}

    class FakeGitHub:
        def open_pull_request(
            self, repo: Repo, *, head: str, base: str, title: str, body: str
        ) -> Any:
            opened.update(head=head, base=base, title=title, repo=repo.full_name)
            from stromboli.pr import PullRequest

            return PullRequest(url="https://github.com/acme/widgets/pull/9", number=9)

    wt = _worktree(tmp_path)
    result = publish_pr(notion, FakeGitHub(), wt, _task(), run=git)

    assert isinstance(result, PublishResult)
    assert result.empty_diff is False
    assert result.pr_url == "https://github.com/acme/widgets/pull/9"
    # Branch was pushed and the PR opened on the worktree branch against main.
    assert git.ran("push")
    assert opened["head"] == wt.branch
    assert opened["base"] == "main"
    # PR URL written back to the task.
    assert notion.pr_url == "https://github.com/acme/widgets/pull/9"


def test_publish_pr_does_not_open_empty_pr(tmp_path: Any) -> None:
    git = RecordingGit(status="", ahead="0")  # nothing to ship
    notion = FakeNotion()

    class ExplodingGitHub:
        def open_pull_request(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("must not open a PR for an empty diff")

    result = publish_pr(notion, ExplodingGitHub(), _worktree(tmp_path), _task(), run=git)

    assert result.empty_diff is True
    assert result.pr_url is None
    assert not git.ran("push")  # nothing pushed
    assert notion.pr_url is None  # no PR field written
