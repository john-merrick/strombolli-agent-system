"""Open the pull request and write it back to Notion (PRD §6.7).

Reached only behind the verifier gate (a ``pass``), the PR node turns the
worktree branch into a reviewable PR:

1. **Detect changes** — :func:`branch_has_changes` is true when the branch has
   commits ahead of ``main`` *or* uncommitted working-tree changes. An empty
   branch must never become an empty PR.
2. **Commit & push** — any residual uncommitted changes are committed (the
   coding node's agent commits as it works, so this is belt-and-braces) and the
   branch is pushed.
3. **Open the PR** — against ``main`` with a title/body derived from the task
   and its Spec, via the GitHub REST API.
4. **Write back** — the PR URL is written to the task's ``PR`` field.

When there are no changes, :func:`publish_pr` opens no PR and returns
``empty_diff=True``; the caller routes the task to ``Review`` with an
explanatory note (PRD §6.7).

Git and GitHub are reached through injected seams — a stdout-returning git
runner and an ``httpx`` client — so the orchestration is unit-testable without
a network or a real repository.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx

from stromboli.integrations.notion import Task
from stromboli.sandbox.runner import Worktree

logger = logging.getLogger(__name__)

#: Default base branch PRs are opened against.
DEFAULT_BASE_BRANCH: Final = "main"
#: Commit message for the worker's belt-and-braces final commit.
_FINAL_COMMIT_MESSAGE: Final = "chore: stromboli build artifacts"

#: Git runner returning stdout; raises :class:`GitCommandError` on failure.
GitRunner = Callable[[Sequence[str]], str]


class GitCommandError(RuntimeError):
    """A git invocation exited non-zero."""


def _run_git(args: Sequence[str]) -> str:
    """Default :data:`GitRunner`: shell out to ``git`` and return its stdout."""
    cmd = ["git", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    if proc.returncode != 0:
        raise GitCommandError(
            f"`{' '.join(cmd)}` exited {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout


@dataclass(frozen=True)
class PullRequest:
    """A GitHub pull request the worker opened."""

    url: str
    number: int


@dataclass(frozen=True)
class PullRequestState:
    """A watched PR's live state for the feedback loop (§ PR feedback design)."""

    number: int
    head_sha: str
    #: ``open`` | ``closed``; ``merged`` is ``closed`` + ``merged=True``.
    state: str
    merged: bool


@dataclass(frozen=True)
class CheckSummary:
    """The aggregate CI verdict for a PR's head SHA."""

    #: ``success`` | ``failure`` | ``pending`` | ``none`` (no checks configured).
    conclusion: str
    #: Names of the failing check runs (for the fix prompt).
    failing: tuple[str, ...] = ()


@dataclass(frozen=True)
class Comment:
    """A PR issue/review comment (for the reviewer-feedback signal)."""

    body: str
    created_at: str
    author: str


@dataclass(frozen=True)
class PublishResult:
    """Outcome of :func:`publish_pr`."""

    #: The opened PR's URL, or ``None`` when nothing was shipped.
    pr_url: str | None
    #: True when the branch had no changes, so no PR was opened.
    empty_diff: bool
    #: The opened PR's number (for the feedback loop), or ``None``.
    pr_number: int | None = None


class GitHubGateway(Protocol):
    """The slice of :class:`GitHubClient` the orchestration needs."""

    def open_pull_request(
        self, repo: Any, *, head: str, base: str, title: str, body: str
    ) -> PullRequest: ...


class PRFeedbackGateway(Protocol):
    """The GitHub surface the PR feedback loop reads/writes (§ design)."""

    def get_pull_request(self, repo: Any, number: int) -> PullRequestState: ...

    def check_summary(self, repo: Any, sha: str) -> CheckSummary: ...

    def failing_logs(self, repo: Any, sha: str) -> str: ...

    def list_comments(self, repo: Any, number: int, *, since: str | None) -> list[
        Comment
    ]: ...

    def add_comment(self, repo: Any, number: int, body: str) -> None: ...


# --------------------------------------------------------------------------- #
# Pure derivation                                                             #
# --------------------------------------------------------------------------- #


def derive_pr_title(task: Task) -> str:
    """A concise PR title derived from the task name."""
    name = task.name.strip() or "Stromboli build"
    return name


def derive_pr_body(task: Task) -> str:
    """A PR body carrying the task Spec and a reference back to the task page."""
    spec = task.spec.strip() or "_No spec provided._"
    return (
        f"Automated build by Stromboli for task `{task.page_id}`.\n\n"
        f"## Spec\n\n{spec}\n\n"
        f"---\n"
        f"_Opened by the Stromboli worker. Review and merge per the autonomy "
        f"rules._"
    )


# --------------------------------------------------------------------------- #
# GitHub client                                                               #
# --------------------------------------------------------------------------- #


class GitHubClient:
    """Minimal GitHub REST client for opening pull requests."""

    BASE_URL: Final = "https://api.github.com"

    def __init__(self, token: str, *, client: httpx.Client | None = None) -> None:
        self._token = token
        self._client = client or httpx.Client(base_url=self.BASE_URL)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def open_pull_request(
        self, repo: Any, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        """Open a PR on ``repo`` from ``head`` into ``base`` and return it.

        Idempotent: if a PR already exists for ``head`` (GitHub 422), reuse it
        rather than failing — a re-run of the same task shouldn't crash.
        """
        resp = self._client.post(
            f"/repos/{repo.full_name}/pulls",
            headers=self._headers,
            json={"title": title, "body": body, "head": head, "base": base},
        )
        if resp.status_code == 422:
            existing = self._find_open_pr(repo, head)
            if existing is not None:
                return existing
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return PullRequest(url=data["html_url"], number=int(data["number"]))

    def _find_open_pr(self, repo: Any, head: str) -> PullRequest | None:
        """Look up an already-open PR for ``head`` (``owner:branch``)."""
        owner = getattr(repo, "owner", None)
        head_q = f"{owner}:{head}" if owner else head
        resp = self._client.get(
            f"/repos/{repo.full_name}/pulls",
            headers=self._headers,
            params={"head": head_q, "state": "open"},
        )
        if resp.status_code != 200:
            return None
        items = resp.json()
        if isinstance(items, list) and items:
            pr = items[0]
            return PullRequest(url=pr["html_url"], number=int(pr["number"]))
        return None

    # -- PR feedback loop (§ design) --------------------------------------- #

    def get_pull_request(self, repo: Any, number: int) -> PullRequestState:
        """The live open/closed/merged state + head SHA of a PR."""
        resp = self._client.get(
            f"/repos/{repo.full_name}/pulls/{number}", headers=self._headers
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return PullRequestState(
            number=number,
            head_sha=str(data["head"]["sha"]),
            state=str(data["state"]),
            merged=bool(data.get("merged")),
        )

    def check_summary(self, repo: Any, sha: str) -> CheckSummary:
        """Aggregate the check-runs for ``sha`` into one CI verdict.

        ``none`` when the repo has no checks configured; ``pending`` while any
        run is unfinished; ``failure`` if any completed run did not succeed;
        else ``success``.
        """
        resp = self._client.get(
            f"/repos/{repo.full_name}/commits/{sha}/check-runs", headers=self._headers
        )
        if resp.status_code != 200:
            return CheckSummary(conclusion="none")
        runs = resp.json().get("check_runs", [])
        if not runs:
            return CheckSummary(conclusion="none")
        if any(r.get("status") != "completed" for r in runs):
            return CheckSummary(conclusion="pending")
        failing = tuple(
            str(r.get("name", "check"))
            for r in runs
            if r.get("conclusion") not in ("success", "neutral", "skipped")
        )
        return CheckSummary(
            conclusion="failure" if failing else "success", failing=failing
        )

    def failing_logs(self, repo: Any, sha: str) -> str:
        """Best-effort text of what failed for ``sha`` — check-run outputs.

        Uses the check-run ``output`` (title/summary/annotations), which needs
        no extra scopes; full Actions job logs would require ``actions:read``.
        Returns "" when nothing legible is available.
        """
        resp = self._client.get(
            f"/repos/{repo.full_name}/commits/{sha}/check-runs", headers=self._headers
        )
        if resp.status_code != 200:
            return ""
        parts: list[str] = []
        for run in resp.json().get("check_runs", []):
            if run.get("conclusion") in ("success", "neutral", "skipped", None):
                continue
            out = run.get("output") or {}
            chunk = "\n".join(
                str(x) for x in (out.get("title"), out.get("summary"), out.get("text"))
                if x
            )
            if chunk:
                parts.append(f"### {run.get('name', 'check')}\n{chunk}")
        return "\n\n".join(parts)[:6000]

    def list_comments(
        self, repo: Any, number: int, *, since: str | None
    ) -> list[Comment]:
        """Issue comments on the PR after ``since`` (ISO), oldest first."""
        params: dict[str, Any] = {"sort": "created", "direction": "asc"}
        if since:
            params["since"] = since
        resp = self._client.get(
            f"/repos/{repo.full_name}/issues/{number}/comments",
            headers=self._headers,
            params=params,
        )
        if resp.status_code != 200:
            return []
        return [
            Comment(
                body=str(c.get("body", "")),
                created_at=str(c.get("created_at", "")),
                author=str((c.get("user") or {}).get("login", "")),
            )
            for c in resp.json()
        ]

    def add_comment(self, repo: Any, number: int, body: str) -> None:
        """Post a comment on the PR (best-effort status write-back)."""
        resp = self._client.post(
            f"/repos/{repo.full_name}/issues/{number}/comments",
            headers=self._headers,
            json={"body": body},
        )
        resp.raise_for_status()


# --------------------------------------------------------------------------- #
# Git helpers                                                                 #
# --------------------------------------------------------------------------- #


def _git_in(worktree: Worktree, *args: str) -> list[str]:
    """Build a ``git -C <worktree> …`` argv."""
    return ["-C", str(worktree.path), *args]


def branch_has_changes(
    worktree: Worktree,
    *,
    base: str = DEFAULT_BASE_BRANCH,
    run: GitRunner | None = None,
) -> bool:
    """Whether the branch has anything to ship (commits ahead or dirty tree)."""
    git = run or _run_git
    porcelain = git(_git_in(worktree, "status", "--porcelain")).strip()
    if porcelain:
        return True
    ahead = git(
        _git_in(worktree, "rev-list", "--count", f"origin/{base}..HEAD")
    ).strip()
    try:
        return int(ahead or "0") > 0
    except ValueError:  # pragma: no cover - defensive
        return False


def commit_and_push(
    worktree: Worktree,
    *,
    message: str = _FINAL_COMMIT_MESSAGE,
    run: GitRunner | None = None,
) -> None:
    """Commit any residual changes and push the branch to ``origin``."""
    git = run or _run_git
    git(_git_in(worktree, "add", "-A"))
    if git(_git_in(worktree, "status", "--porcelain")).strip():
        git(_git_in(worktree, "commit", "-m", message))
    # Force-push — the branch is a per-task, Stromboli-owned build branch
    # (deterministic name only Stromboli writes); a stale remote one from a prior
    # run must be replaced, not block the PR with a non-fast-forward rejection.
    git(_git_in(worktree, "push", "-u", "--force", "origin", worktree.branch))


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #


def publish_pr(
    notion: Any,
    github: GitHubGateway,
    worktree: Worktree,
    task: Task,
    *,
    base: str = DEFAULT_BASE_BRANCH,
    run: GitRunner | None = None,
) -> PublishResult:
    """Commit, push, open a PR, and write its URL back — unless the diff is empty.

    Returns a :class:`PublishResult`. When the branch produced no changes, no PR
    is opened (``empty_diff=True``) and the caller routes the task to ``Review``.
    """
    if not branch_has_changes(worktree, base=base, run=run):
        logger.info(
            "No changes on %s for task %s; skipping PR.",
            worktree.branch,
            task.page_id,
        )
        return PublishResult(pr_url=None, empty_diff=True)

    commit_and_push(worktree, run=run)
    pr = github.open_pull_request(
        worktree.repo,
        head=worktree.branch,
        base=base,
        title=derive_pr_title(task),
        body=derive_pr_body(task),
    )
    logger.info("Opened PR %s for task %s", pr.url, task.page_id)
    if notion is not None:  # a CLI task has no page to write the URL back to
        notion.update_task(task.page_id, pr_url=pr.url)
    return PublishResult(pr_url=pr.url, empty_diff=False, pr_number=pr.number)


__all__ = [
    "DEFAULT_BASE_BRANCH",
    "CheckSummary",
    "Comment",
    "GitCommandError",
    "GitHubClient",
    "GitHubGateway",
    "GitRunner",
    "PRFeedbackGateway",
    "PullRequest",
    "PullRequestState",
    "PublishResult",
    "branch_has_changes",
    "commit_and_push",
    "derive_pr_body",
    "derive_pr_title",
    "publish_pr",
]
