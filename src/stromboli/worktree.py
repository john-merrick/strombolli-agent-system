"""Per-task isolated git worktree management (US-005).

Each task is built in its own git worktree so concurrent / sequential tasks
never share a working tree. The flow is:

1. Resolve ``owner/repo`` (the caller passes the :class:`~stromboli.notion.Repo`
   already resolved from the task's Project page).
2. Ensure a local bare-ish clone exists under ``WORKSPACE_ROOT`` and fetch the
   latest base branch (``main``).
3. Add a fresh worktree on a **deterministically-named** branch
   (``stromboli/<task-id>-<slug>``) based on ``origin/<base>``.
4. Remove the worktree on completion *or* failure — the :meth:`WorktreeManager.worktree`
   context manager guarantees cleanup in a ``finally`` block.

Git is invoked through an injected ``run`` callable (default: a real
``subprocess`` runner) so command derivation and cleanup-on-error are unit
testable without the network or a real repository.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from stromboli.notion import Repo

logger = logging.getLogger(__name__)

#: Default base branch the worktree branches off of.
DEFAULT_BASE_BRANCH: Final = "main"
#: Prefix for every worker-created branch.
BRANCH_PREFIX: Final = "stromboli"
#: Max length of the slug portion of a branch / directory name.
SLUG_MAX_LEN: Final = 50

#: Git runner: given an argv (sans the leading ``git``), run it, raising
#: :class:`GitError` on a non-zero exit.
GitRunner = Callable[[Sequence[str]], None]


class GitError(RuntimeError):
    """A git invocation exited non-zero."""


def _run_git(args: Sequence[str]) -> None:
    """Default :data:`GitRunner`: shell out to ``git`` and raise on failure."""
    cmd = ["git", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    if proc.returncode != 0:
        raise GitError(
            f"`{' '.join(cmd)}` exited {proc.returncode}: {proc.stderr.strip()}"
        )


# --------------------------------------------------------------------------- #
# Pure name derivation                                                        #
# --------------------------------------------------------------------------- #


def slugify(text: str) -> str:
    """Lowercase, hyphenate, and truncate ``text`` into a git-safe slug.

    Non-alphanumeric runs collapse to a single ``-``; leading/trailing hyphens
    are stripped; the result is capped at :data:`SLUG_MAX_LEN` with no trailing
    hyphen. Symbol-only input yields ``""``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > SLUG_MAX_LEN:
        slug = slug[:SLUG_MAX_LEN].rstrip("-")
    return slug


def derive_branch_name(task_id: str, task_name: str) -> str:
    """Deterministic branch name ``stromboli/<task-id>-<slug>``.

    Identical ``(task_id, task_name)`` always yields the same branch. When the
    name has no usable slug, the branch is just ``stromboli/<task-id>``.
    """
    base = f"{BRANCH_PREFIX}/{task_id}"
    slug = slugify(task_name)
    return f"{base}-{slug}" if slug else base


def clone_url(repo: Repo, token: str | None = None) -> str:
    """HTTPS clone URL for ``repo``; embeds ``token`` for authenticated access."""
    if token:
        return f"https://x-access-token:{token}@github.com/{repo.full_name}.git"
    return f"https://github.com/{repo.full_name}.git"


# --------------------------------------------------------------------------- #
# Worktree manager                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Worktree:
    """A prepared, isolated worktree for one task build."""

    path: Path
    branch: str
    repo: Repo
    clone_path: Path


class WorktreeManager:
    """Creates and tears down isolated per-task git worktrees under a root."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        run: GitRunner | None = None,
        token: str | None = None,
        base_branch: str = DEFAULT_BASE_BRANCH,
    ) -> None:
        self._root = Path(workspace_root)
        self._run = run or _run_git
        self._token = token
        self._base = base_branch

    # -- clone -------------------------------------------------------------- #

    def _clone_path(self, repo: Repo) -> Path:
        """Where the shared local clone for ``repo`` lives (``root/owner/repo``)."""
        return self._root / repo.owner / repo.repo

    def ensure_clone(self, repo: Repo) -> Path:
        """Ensure a local clone exists and the base branch is up to date.

        Clones on first use; otherwise fetches the latest base branch into the
        existing clone. Returns the clone path.
        """
        clone = self._clone_path(repo)
        if (clone / ".git").exists():
            logger.info("Fetching %s into existing clone %s", self._base, clone)
            self._run(["-C", str(clone), "fetch", "origin", self._base])
        else:
            logger.info("Cloning %s into %s", repo.full_name, clone)
            clone.parent.mkdir(parents=True, exist_ok=True)
            self._run(["clone", clone_url(repo, self._token), str(clone)])
        return clone

    # -- worktree lifecycle ------------------------------------------------- #

    def _worktree_path(self, repo: Repo, task_id: str, task_name: str) -> Path:
        """Deterministic on-disk location for a task's worktree."""
        slug = slugify(task_name)
        leaf = f"{task_id}-{slug}" if slug else task_id
        return self._root / "worktrees" / repo.owner / repo.repo / leaf

    @contextmanager
    def worktree(
        self, repo: Repo, task_id: str, task_name: str
    ) -> Iterator[Worktree]:
        """Prepare an isolated worktree for a task, cleaning up on exit.

        Yields a :class:`Worktree`. The worktree (and its branch checkout) is
        removed when the ``with`` block exits, whether it completes normally or
        raises — so a failed build never leaves a stale worktree behind.
        """
        clone = self.ensure_clone(repo)
        branch = derive_branch_name(task_id, task_name)
        wt_path = self._worktree_path(repo, task_id, task_name)
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Adding worktree %s on branch %s", wt_path, branch)
        self._run(
            [
                "-C",
                str(clone),
                "worktree",
                "add",
                "-b",
                branch,
                str(wt_path),
                f"origin/{self._base}",
            ]
        )
        worktree = Worktree(
            path=wt_path, branch=branch, repo=repo, clone_path=clone
        )
        try:
            yield worktree
        finally:
            self._remove_worktree(clone, wt_path)

    def _remove_worktree(self, clone: Path, wt_path: Path) -> None:
        """Remove a worktree, falling back to ``rmtree`` if git can't."""
        try:
            self._run(
                ["-C", str(clone), "worktree", "remove", "--force", str(wt_path)]
            )
        except GitError as exc:
            logger.warning(
                "git worktree remove failed for %s (%s); pruning manually",
                wt_path,
                exc,
            )
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)


__all__ = [
    "BRANCH_PREFIX",
    "DEFAULT_BASE_BRANCH",
    "GitError",
    "GitRunner",
    "Worktree",
    "WorktreeManager",
    "clone_url",
    "derive_branch_name",
    "slugify",
]
