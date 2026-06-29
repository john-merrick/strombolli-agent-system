"""Tests for PR derivation and the publish orchestration (dry-run friendly)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stromboli.integrations.github import (
    PullRequest,
    branch_has_changes,
    derive_pr_body,
    derive_pr_title,
    publish_pr,
)
from stromboli.integrations.notion import Repo, Task
from stromboli.sandbox.runner import Worktree


def _task() -> Task:
    return Task(
        page_id="page-1",
        name="Add a flag",
        project_ids=("proj-1",),
        status="To do",
        spec="add a --verbose flag",
        assigned_to="Agent",
        ready=True,
        needs_review=False,
        pr_url=None,
        cost=None,
        tokens=None,
    )


def _worktree() -> Worktree:
    repo = Repo(owner="o", repo="r")
    return Worktree(
        path=Path("/tmp/wt"), branch="stromboli/page-1-add-a-flag", repo=repo,
        clone_path=Path("/tmp/clone"),
    )


def test_derive_pr_title_and_body() -> None:
    task = _task()
    assert derive_pr_title(task) == "Add a flag"
    body = derive_pr_body(task)
    assert "add a --verbose flag" in body
    assert task.page_id in body


def test_branch_has_changes_dirty_tree() -> None:
    def run(args: Any) -> str:
        return " M file.py\n" if "status" in args else "0"

    assert branch_has_changes(_worktree(), run=run) is True


def test_branch_has_changes_clean() -> None:
    def run(args: Any) -> str:
        return "" if "status" in args else "0"

    assert branch_has_changes(_worktree(), run=run) is False


def test_publish_pr_empty_diff_opens_nothing() -> None:
    class NoGitHub:
        def open_pull_request(self, *a: Any, **k: Any) -> PullRequest:
            raise AssertionError("must not open a PR on an empty diff")

    def run(args: Any) -> str:
        return "" if "status" in args else "0"  # clean tree, 0 ahead

    result = publish_pr(object(), NoGitHub(), _worktree(), _task(), run=run)
    assert result.empty_diff is True
    assert result.pr_url is None


def test_open_pr_reuses_existing_on_422() -> None:
    import httpx

    repo = Repo(owner="o", repo="r")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(422, json={"message": "A pull request already exists"})
        # GET /pulls?head=o:branch&state=open → the existing PR
        return httpx.Response(
            200,
            json=[{"html_url": "https://github.com/o/r/pull/3", "number": 3}],
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )
    from stromboli.integrations.github import GitHubClient

    gh = GitHubClient("tok", client=client)
    pr = gh.open_pull_request(
        repo, head="stromboli/x", base="main", title="t", body="b"
    )
    assert pr.url == "https://github.com/o/r/pull/3"  # reused, not crashed


def test_publish_pr_opens_and_writes_back() -> None:
    opened: dict[str, Any] = {}
    written: dict[str, Any] = {}

    class GitHub:
        def open_pull_request(
            self, repo: Any, *, head: str, base: str, title: str, body: str
        ) -> PullRequest:
            opened.update(head=head, base=base, title=title)
            return PullRequest(url="https://github.com/o/r/pull/3", number=3)

    class Notion:
        def update_task(self, page_id: str, *, pr_url: str | None = None) -> None:
            written.update(page_id=page_id, pr_url=pr_url)

    calls: list[str] = []

    def run(args: Any) -> str:
        calls.append(" ".join(args))
        if "status" in args:
            return " M f.py\n"  # dirty → has changes; then clean after commit
        return "1"

    result = publish_pr(Notion(), GitHub(), _worktree(), _task(), run=run)
    assert result.pr_url == "https://github.com/o/r/pull/3"
    assert opened["head"] == "stromboli/page-1-add-a-flag"
    assert written["pr_url"] == "https://github.com/o/r/pull/3"
