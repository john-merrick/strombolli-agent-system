"""Tests for the PR / Commit node (PRD §6.7) — dry-run and live."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from stromboli.integrations.github import PullRequest
from stromboli.nodes.pr import make_pr
from stromboli.state import Spec, StromboliState
from tests.nodes._fakes import FakeNotion, make_worktree


def _state() -> StromboliState:
    return StromboliState(
        task_id="page-1", source="notion", raw_request="add a flag",
        spec=Spec(goal="add --verbose", acceptance_criteria=["prints debug"]),
        code_diff="diff --git a/x b/x\n+ok",
    )


class FakeGitHub:
    def __init__(self) -> None:
        self.opened: dict[str, Any] = {}

    def open_pull_request(
        self, repo: Any, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        self.opened = {"head": head, "base": base, "title": title, "body": body}
        return PullRequest(url="https://github.com/o/r/pull/5", number=5)


def _dirty_git(args: Sequence[str]) -> str:
    # status --porcelain → dirty (has changes); everything else → benign.
    return " M file.py\n" if "status" in args else "1"


def test_dry_run_opens_no_pr() -> None:
    out = make_pr(dry_run=True)(_state())
    assert out == {"pr_url": None, "status": "pr"}


def test_live_opens_pr_and_writes_back() -> None:
    github = FakeGitHub()
    notion = FakeNotion()
    node = make_pr(
        github=github, notion=notion, worktree_for=lambda _s: make_worktree(),
        dry_run=False, git_run=_dirty_git,
    )
    out = node(_state())
    assert out["pr_url"] == "https://github.com/o/r/pull/5"
    assert out["status"] == "pr"
    # PR title comes from the spec goal; the URL is written back to Notion.
    assert github.opened["title"] == "add --verbose"
    assert notion.pr_writes == [("page-1", "https://github.com/o/r/pull/5")]


def test_live_empty_diff_opens_no_pr() -> None:
    github = FakeGitHub()

    def clean_git(args: Sequence[str]) -> str:
        return "" if "status" in args else "0"  # clean tree, 0 ahead

    out = make_pr(
        github=github, notion=FakeNotion(), worktree_for=lambda _s: make_worktree(),
        dry_run=False, git_run=clean_git,
    )(_state())
    assert out["pr_url"] is None
    assert github.opened == {}  # nothing opened on an empty diff
