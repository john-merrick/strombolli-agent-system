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
from typing import Final, Protocol

from stromboli.integrations.notion import Repo

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
        # ``-B`` (not ``-b``) so a re-run of the same task — whose branch name is
        # deterministic — resets the branch to the base rather than failing on
        # "branch already exists". Clone-per-task semantics make this safe.
        self._run(
            [
                "-C",
                str(clone),
                "worktree",
                "add",
                "-B",
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

    def ensure(self, repo: Repo, task_id: str, task_name: str) -> Worktree:
        """Ensure a **durable** per-task worktree exists (idempotent) and return it.

        Unlike :meth:`worktree`, this never removes the worktree — the investigate
        loop needs it to survive a suspend so a resume/probe can rebuild on the
        coder's prior work. A re-call **reuses** the existing worktree (keeping
        those changes) instead of resetting it; only a first call creates one (from
        the base branch). Terminal cleanup is explicit via :meth:`remove`.
        """
        clone = self.ensure_clone(repo)
        branch = derive_branch_name(task_id, task_name)
        wt_path = self._worktree_path(repo, task_id, task_name)
        if (wt_path / ".git").exists():  # already provisioned — keep its contents
            return Worktree(path=wt_path, branch=branch, repo=repo, clone_path=clone)
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Adding durable worktree %s on branch %s", wt_path, branch)
        self._run(
            ["-C", str(clone), "worktree", "add", "-B", branch, str(wt_path),
             f"origin/{self._base}"]
        )
        return Worktree(path=wt_path, branch=branch, repo=repo, clone_path=clone)

    def remove(self, repo: Repo, task_id: str, task_name: str) -> None:
        """Tear down a task's durable worktree (terminal outcome / expiry cleanup)."""
        clone = self._clone_path(repo)
        wt_path = self._worktree_path(repo, task_id, task_name)
        if wt_path.exists():
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


# --------------------------------------------------------------------------- #
# Sandboxed test runner — execute the target repo's checks in isolation        #
# --------------------------------------------------------------------------- #

#: The default test command run in the sandbox (the build's only oracle, §6.4).
DEFAULT_TEST_COMMAND: Final = ("python", "-m", "pytest", "-q")
#: The default Docker image the sandbox runs against (see docker/sandbox.Dockerfile).
DEFAULT_SANDBOX_IMAGE: Final = "stromboli-sandbox:latest"
#: Cap on captured test output so a runaway log never blows up the state object.
_MAX_OUTPUT_CHARS: Final = 20_000

#: Runs ``argv`` with ``cwd`` and returns ``(exit_code, combined_output)``.
CommandRunner = Callable[[Sequence[str], Path], tuple[int, str]]


class TestSandbox(Protocol):
    """The sandbox seam the coding node depends on (run the tests in isolation)."""

    def run_tests(
        self, worktree_path: str | Path, command: Sequence[str] = ...
    ) -> SandboxResult: ...


@dataclass(frozen=True)
class SandboxResult:
    """The outcome of one sandboxed test run (PRD §6.4 — the inner-loop oracle)."""

    passed: bool
    output: str
    exit_code: int


def _truncate(text: str) -> str:
    """Keep the *tail* of the output — failures live at the end of a test log."""
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return "…(truncated)…\n" + text[-_MAX_OUTPUT_CHARS:]


def _subprocess_runner(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    """Default :data:`CommandRunner`: run ``argv`` capturing merged stdout+stderr."""
    proc = subprocess.run(  # noqa: S603
        list(argv), cwd=cwd, capture_output=True, text=True
    )
    return proc.returncode, proc.stdout + proc.stderr


class SandboxRunner:
    """Runs the target repo's checks isolated from the host (PRD §4 / §6.4).

    By default it wraps the command in ``docker run --rm`` against the worktree
    (mounted read-write, network disabled) so test execution can't touch the
    host. ``use_docker=False`` runs the command directly — used in tests and as
    a fallback where Docker is unavailable. The command runner is injected so the
    control flow is unit-testable without launching Docker.
    """

    def __init__(
        self,
        *,
        image: str = DEFAULT_SANDBOX_IMAGE,
        run: CommandRunner | None = None,
        use_docker: bool = True,
    ) -> None:
        self._image = image
        self._run = run or _subprocess_runner
        self._use_docker = use_docker

    def _container_name(self, worktree_path: Path) -> str:
        """A deterministic, inspectable container name per worktree."""
        leaf = re.sub(r"[^a-zA-Z0-9_.-]+", "-", worktree_path.name)[:60].strip("-")
        return f"stromboli-sandbox-{leaf or 'run'}"

    def _docker_argv(self, worktree_path: Path, command: Sequence[str]) -> list[str]:
        """Wrap ``command`` in an isolated Docker container — named + labelled so
        it's visible in ``docker ps -a`` / ``docker logs`` (PRD §8 traceability).

        Deliberately NOT ``--rm`` so the finished container persists for
        inspection; a stale one of the same name is removed before each run.
        """
        return [
            "docker",
            "run",
            "--name",
            self._container_name(worktree_path),
            "--label",
            "stromboli=sandbox",
            "--network",
            "none",
            "-v",
            f"{worktree_path}:/work",
            "-w",
            "/work",
            self._image,
            *command,
        ]

    def run_tests(
        self,
        worktree_path: str | Path,
        command: Sequence[str] = DEFAULT_TEST_COMMAND,
    ) -> SandboxResult:
        """Run ``command`` against ``worktree_path`` and capture pass/fail+output."""
        path = Path(worktree_path)
        if self._use_docker:
            name = self._container_name(path)
            # Clear a prior container of this name so the run isn't blocked, and
            # so `docker ps -a` shows only the latest attempt (best-effort).
            self._run(["docker", "rm", "-f", name], path)
            argv = self._docker_argv(path, command)
        else:
            argv = list(command)
        logger.info("Sandbox: %s", " ".join(argv))
        exit_code, output = self._run(argv, path)
        # pytest exit 5 = "no tests collected" — not a failure of the change
        # itself; the verifier judges coverage. Treat 0 and 5 as passing.
        passed = exit_code in (0, 5)
        if exit_code == 5:
            output = f"{output}\n(no tests collected — exit 5)"
        return SandboxResult(
            passed=passed, output=_truncate(output), exit_code=exit_code
        )


__all__ = [
    "BRANCH_PREFIX",
    "DEFAULT_BASE_BRANCH",
    "DEFAULT_SANDBOX_IMAGE",
    "DEFAULT_TEST_COMMAND",
    "CommandRunner",
    "GitError",
    "GitRunner",
    "SandboxResult",
    "SandboxRunner",
    "TestSandbox",
    "Worktree",
    "WorktreeManager",
    "clone_url",
    "derive_branch_name",
    "slugify",
]
