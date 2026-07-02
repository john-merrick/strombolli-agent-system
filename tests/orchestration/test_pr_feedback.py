"""Tests for the PR feedback loop sweep + fix cycle (§ design)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stromboli.integrations.github import CheckSummary, Comment, PullRequestState
from stromboli.orchestration.pr_feedback import (
    MAX_FIX_ROUNDS,
    STROMBOLI_MARKER,
    PRFeedbackService,
)
from stromboli.orchestration.pr_index import ESCALATED, WATCHING, PRIndex, PRWatch
from stromboli.sandbox.runner import SandboxResult, Worktree


class FakeGitHub:
    def __init__(self, *, pr: PullRequestState, checks: CheckSummary,
                 comments: list[Comment] | None = None, logs: str = "boom") -> None:
        self._pr = pr
        self._checks = checks
        self._comments = comments or []
        self._logs = logs
        self.comments_posted: list[str] = []

    def get_pull_request(self, repo: Any, number: int) -> PullRequestState:
        return self._pr

    def check_summary(self, repo: Any, sha: str) -> CheckSummary:
        return self._checks

    def failing_logs(self, repo: Any, sha: str) -> str:
        return self._logs

    def list_comments(self, repo: Any, number: int, *, since: str | None) -> list[Comment]:
        return list(self._comments)

    def add_comment(self, repo: Any, number: int, body: str) -> None:
        self.comments_posted.append(body)


class FakeCoder:
    def __init__(self) -> None:
        self.resumed: list[str | None] = []

    def run(self, prompt: str, cwd: Any, *, resume: str | None = None) -> Any:
        self.resumed.append(resume)
        from stromboli.llm.coder import CoderRun
        return CoderRun(
            diff="diff --git a/x b/x\n+fix", final_text="fixed", turns=2,
            session_id="sess-2", subtype="success", is_error=False,
            cost_usd=None, usage=None, turn_records=(),
        )


class FakeSandbox:
    def __init__(self, passed: bool = True) -> None:
        self._passed = passed

    def run_tests(self, path: Any, command: Any = ()) -> SandboxResult:
        return SandboxResult(passed=self._passed, output="ok", exit_code=0)


class FakeGateway:
    """Returns a Verdict payload for the verifier's structured call."""

    def __init__(self, decision: str = "pass") -> None:
        self._decision = decision
        self.last_usage: dict[str, Any] | None = None

    def structured(self, *, model: str, system: str, user: str, schema: Any) -> Any:
        return schema.model_validate(
            {"decision": self._decision, "reason": "r", "coverage_note": "c"}
        )


class FakeWM:
    def ensure_from_branch(self, repo: Any, task_id: str, goal: str, branch: str) -> Worktree:
        return Worktree(path=Path("/tmp/wt"), branch=branch, repo=repo,
                        clone_path=Path("/tmp/clone"))


def _svc(tmp_path: Path, github: FakeGitHub, *, coder: Any = None,
         gateway: Any = None, escalations: list[Any] | None = None,
         pushes: list[str] | None = None) -> tuple[PRFeedbackService, PRIndex]:
    idx = PRIndex(tmp_path / "prs.db")
    esc = escalations if escalations is not None else []
    import stromboli.orchestration.pr_feedback as mod
    if pushes is not None:
        mod.commit_and_push = lambda wt: pushes.append(wt.branch)  # type: ignore
    svc = PRFeedbackService(
        index=idx, github=github, coder=coder or FakeCoder(),
        sandbox=FakeSandbox(), gateway=gateway or FakeGateway("pass"),
        verifier_model="gemini", worktree_manager=FakeWM(),
        now=lambda: "2026-07-02T12:00:00+00:00",
        on_escalate=lambda w, r: esc.append((w.task_id, r)),
    )
    return svc, idx


def _register(idx: PRIndex, **over: object) -> None:
    base = dict(task_id="t1", repo="o/r", branch="stromboli/t1-x", pr_number=5,
                pr_url="u", goal="do x", session_id="sess-1")
    base.update(over)
    idx.register(PRWatch(**base), now="t0")  # type: ignore[arg-type]


def test_merged_pr_stops_being_watched(tmp_path: Path) -> None:
    gh = FakeGitHub(pr=PullRequestState(5, "sha1", "closed", merged=True),
                    checks=CheckSummary("success"))
    svc, idx = _svc(tmp_path, gh)
    _register(idx)
    svc.sweep()
    assert idx.active() == []


def test_green_ci_no_comments_is_noop(tmp_path: Path) -> None:
    gh = FakeGitHub(pr=PullRequestState(5, "sha1", "open", merged=False),
                    checks=CheckSummary("success"))
    svc, idx = _svc(tmp_path, gh)
    _register(idx)
    acted = svc.sweep()
    assert acted == [] and idx.active()[0].state == WATCHING


def test_failing_ci_triggers_fix_and_pushes(tmp_path: Path) -> None:
    gh = FakeGitHub(pr=PullRequestState(5, "shaX", "open", merged=False),
                    checks=CheckSummary("failure", failing=("pytest",)))
    coder = FakeCoder()
    pushes: list[str] = []
    svc, idx = _svc(tmp_path, gh, coder=coder, pushes=pushes)
    _register(idx)
    acted = svc.sweep()
    assert acted == ["t1"]
    assert coder.resumed == ["sess-1"]           # resumed the stored session
    assert pushes == ["stromboli/t1-x"]           # fix pushed to the same branch
    row = idx.active()[0]
    assert row.fix_rounds == 1 and row.last_ci_sha == "shaX"
    assert row.session_id == "sess-2"             # session advanced


def test_new_comment_triggers_fix(tmp_path: Path) -> None:
    gh = FakeGitHub(pr=PullRequestState(5, "sha1", "open", merged=False),
                    checks=CheckSummary("success"),
                    comments=[Comment("please rename the function", "t1", "isaac")])
    pushes: list[str] = []
    svc, idx = _svc(tmp_path, gh, pushes=pushes)
    _register(idx)
    assert svc.sweep() == ["t1"]
    assert pushes == ["stromboli/t1-x"]


def test_own_marked_comment_is_ignored(tmp_path: Path) -> None:
    gh = FakeGitHub(pr=PullRequestState(5, "sha1", "open", merged=False),
                    checks=CheckSummary("success"),
                    comments=[Comment(f"{STROMBOLI_MARKER} pushed a fix", "t1", "bot")])
    pushes: list[str] = []
    svc, idx = _svc(tmp_path, gh, pushes=pushes)
    _register(idx)
    assert svc.sweep() == [] and pushes == []


def test_skip_marker_opts_out(tmp_path: Path) -> None:
    gh = FakeGitHub(pr=PullRequestState(5, "sha1", "open", merged=False),
                    checks=CheckSummary("failure", failing=("pytest",)),
                    comments=[Comment("stromboli: skip this one", "t1", "isaac")])
    svc, idx = _svc(tmp_path, gh)
    _register(idx)
    svc.sweep()
    assert idx.active() == []                      # closed, no fix


def test_pending_ci_waits(tmp_path: Path) -> None:
    gh = FakeGitHub(pr=PullRequestState(5, "sha1", "open", merged=False),
                    checks=CheckSummary("pending"))
    pushes: list[str] = []
    svc, idx = _svc(tmp_path, gh, pushes=pushes)
    _register(idx)
    assert svc.sweep() == [] and pushes == []


def test_same_sha_not_reprocessed(tmp_path: Path) -> None:
    gh = FakeGitHub(pr=PullRequestState(5, "shaSame", "open", merged=False),
                    checks=CheckSummary("failure", failing=("pytest",)))
    pushes: list[str] = []
    svc, idx = _svc(tmp_path, gh, pushes=pushes)
    _register(idx, last_ci_sha="shaSame")          # already acted on this SHA
    assert svc.sweep() == [] and pushes == []


def test_rounds_exhausted_escalates(tmp_path: Path) -> None:
    gh = FakeGitHub(pr=PullRequestState(5, "shaZ", "open", merged=False),
                    checks=CheckSummary("failure", failing=("pytest",)))
    esc: list[Any] = []
    pushes: list[str] = []
    svc, idx = _svc(tmp_path, gh, escalations=esc, pushes=pushes)
    _register(idx, fix_rounds=MAX_FIX_ROUNDS)
    svc.sweep()
    assert pushes == []                            # no more fixing
    assert esc and esc[0][0] == "t1"
    assert idx.active()[0].state == ESCALATED
    assert any(STROMBOLI_MARKER in c for c in gh.comments_posted)


def test_verifier_reject_escalates(tmp_path: Path) -> None:
    gh = FakeGitHub(pr=PullRequestState(5, "shaV", "open", merged=False),
                    checks=CheckSummary("failure", failing=("pytest",)))
    esc: list[Any] = []
    pushes: list[str] = []
    svc, idx = _svc(tmp_path, gh, gateway=FakeGateway("escalate"),
                    escalations=esc, pushes=pushes)
    _register(idx)
    svc.sweep()
    assert pushes == []                            # rejected fix is not pushed
    assert esc and idx.active()[0].state == ESCALATED
    assert idx.active()[0].fix_rounds == 1         # the attempt still counts
