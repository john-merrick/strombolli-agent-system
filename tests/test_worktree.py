"""Tests for the per-task git worktree manager (US-005).

Git is invoked through an injected runner so command derivation, clone-vs-fetch
selection, and (crucially) cleanup-on-error can be asserted without touching the
network or a real repository. Filesystem state uses pytest's ``tmp_path``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from stromboli.notion import Repo
from stromboli.worktree import (
    GitError,
    Worktree,
    WorktreeManager,
    clone_url,
    derive_branch_name,
    slugify,
)


class RecordingGit:
    """A fake git runner that records argv lists and can be told to fail."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self._fail_on = fail_on

    def __call__(self, args: Sequence[str]) -> None:
        argv = list(args)
        self.calls.append(argv)
        if self._fail_on is not None and self._fail_on in argv:
            raise GitError(f"simulated git failure on {self._fail_on}")

    def commands(self) -> list[str]:
        """The git subcommand (first non-flag token) of each recorded call."""
        result: list[str] = []
        for argv in self.calls:
            tokens = [a for a in argv if not a.startswith("-")]
            # Skip the value following ``-C`` (the repo path).
            if argv[:1] == ["-C"]:
                tokens = tokens[1:]
            result.append(tokens[0] if tokens else "")
        return result


REPO = Repo(owner="snarktank", repo="ralph")


# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #


def test_slugify_lowercases_and_hyphenates() -> None:
    assert slugify("Add OAuth Login!") == "add-oauth-login"


def test_slugify_collapses_and_strips_separators() -> None:
    assert slugify("  Foo   Bar -- baz  ") == "foo-bar-baz"


def test_slugify_truncates_long_input_without_trailing_hyphen() -> None:
    slug = slugify("word " * 40)
    assert len(slug) <= 50
    assert not slug.endswith("-")


def test_slugify_empty_for_symbol_only_input() -> None:
    assert slugify("***") == ""


def test_derive_branch_name_is_deterministic() -> None:
    name = derive_branch_name("US-001", "Add OAuth Login")
    assert name == "stromboli/US-001-add-oauth-login"
    # Same inputs -> same branch (determinism).
    assert derive_branch_name("US-001", "Add OAuth Login") == name


def test_derive_branch_name_without_usable_slug_falls_back_to_id() -> None:
    assert derive_branch_name("US-007", "***") == "stromboli/US-007"


def test_clone_url_plain_and_tokenised() -> None:
    assert clone_url(REPO) == "https://github.com/snarktank/ralph.git"
    assert (
        clone_url(REPO, token="ghp_secret")
        == "https://x-access-token:ghp_secret@github.com/snarktank/ralph.git"
    )


# --------------------------------------------------------------------------- #
# ensure_clone                                                                #
# --------------------------------------------------------------------------- #


def test_ensure_clone_clones_when_absent(tmp_path: Path) -> None:
    git = RecordingGit()
    mgr = WorktreeManager(tmp_path, run=git)

    clone = mgr.ensure_clone(REPO)

    assert clone == tmp_path / "snarktank" / "ralph"
    assert git.commands() == ["clone"]
    # The clone command must carry the derived URL and target path.
    assert clone_url(REPO) in git.calls[0]
    assert str(clone) in git.calls[0]


def test_ensure_clone_fetches_when_present(tmp_path: Path) -> None:
    clone = tmp_path / "snarktank" / "ralph"
    (clone / ".git").mkdir(parents=True)
    git = RecordingGit()
    mgr = WorktreeManager(tmp_path, run=git)

    mgr.ensure_clone(REPO)

    assert git.commands() == ["fetch"]
    # Fetch targets the configured base branch.
    assert "origin" in git.calls[0]
    assert "main" in git.calls[0]


def test_ensure_clone_uses_token_in_url(tmp_path: Path) -> None:
    git = RecordingGit()
    mgr = WorktreeManager(tmp_path, run=git, token="ghp_secret")

    mgr.ensure_clone(REPO)

    assert clone_url(REPO, token="ghp_secret") in git.calls[0]


# --------------------------------------------------------------------------- #
# worktree context manager                                                    #
# --------------------------------------------------------------------------- #


def test_worktree_adds_branch_off_base_and_yields_worktree(tmp_path: Path) -> None:
    git = RecordingGit()
    mgr = WorktreeManager(tmp_path, run=git)

    with mgr.worktree(REPO, "US-001", "Add OAuth Login") as wt:
        assert isinstance(wt, Worktree)
        assert wt.branch == "stromboli/US-001-add-oauth-login"
        assert wt.repo == REPO
        # The add command created the branch off origin/main into the path.
        add = next(c for c in git.calls if "add" in c)
        assert "-b" in add
        assert wt.branch in add
        assert "origin/main" in add
        assert str(wt.path) in add

    # add then remove, in order.
    assert git.commands() == ["clone", "worktree", "worktree"]


def test_worktree_removes_on_normal_exit(tmp_path: Path) -> None:
    git = RecordingGit()
    mgr = WorktreeManager(tmp_path, run=git)

    with mgr.worktree(REPO, "US-001", "Demo") as wt:
        wt_path = wt.path

    remove = next(c for c in git.calls if "remove" in c)
    assert "--force" in remove
    assert str(wt_path) in remove


def test_worktree_removes_on_error_and_propagates(tmp_path: Path) -> None:
    git = RecordingGit()
    mgr = WorktreeManager(tmp_path, run=git)

    boom = RuntimeError("build blew up")
    with pytest.raises(RuntimeError, match="build blew up"):
        with mgr.worktree(REPO, "US-001", "Demo") as wt:
            wt_path = wt.path
            raise boom

    # Cleanup must still have run despite the error.
    assert any("remove" in c for c in git.calls)
    remove = next(c for c in git.calls if "remove" in c)
    assert str(wt_path) in remove


def test_worktree_cleanup_falls_back_to_rmtree_when_git_remove_fails(
    tmp_path: Path,
) -> None:
    git = RecordingGit(fail_on="remove")
    mgr = WorktreeManager(tmp_path, run=git)

    with mgr.worktree(REPO, "US-001", "Demo") as wt:
        # Materialise the worktree dir so the rmtree fallback has something to do.
        wt.path.mkdir(parents=True, exist_ok=True)
        (wt.path / "scratch.txt").write_text("data")
        wt_path = wt.path

    # git remove was attempted and failed, but the directory is gone anyway.
    assert any("remove" in c for c in git.calls)
    assert not wt_path.exists()
