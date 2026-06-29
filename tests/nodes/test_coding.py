"""Tests for the Coding node (PRD §6.4) — fake coder + fake sandbox."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from stromboli.llm.coder import CoderError, CoderRun, RateLimitError, TurnRecord
from stromboli.nodes.coding import _build_prompt, make_coding
from stromboli.sandbox.runner import SandboxResult
from stromboli.state import Spec, StromboliState, Verdict
from tests.nodes._fakes import make_worktree


class FakeCoder:
    def __init__(self, run: CoderRun) -> None:
        self._run = run
        self.calls: list[tuple[str, str | None]] = []

    def run(self, prompt: str, cwd: str | Path, *, resume: str | None = None) -> CoderRun:
        self.calls.append((prompt, resume))
        return self._run


class FakeSandbox:
    def __init__(self, result: SandboxResult) -> None:
        self._result = result
        self.calls: list[Sequence[str]] = []

    def run_tests(
        self, worktree_path: str | Path, command: Sequence[str] = ()
    ) -> SandboxResult:
        self.calls.append(command)
        return self._result


def _run(subtype: str = "success", *, is_error: bool = False) -> CoderRun:
    return CoderRun(
        diff="diff --git a/x b/x\n+ok",
        final_text="done",
        turns=3,
        session_id="sess-1",
        subtype=subtype,
        is_error=is_error,
        cost_usd=0.02,
        usage=None,
        turn_records=(TurnRecord(index=1, tools=("Edit",), usage=None),),
    )


def _state(**over: object) -> StromboliState:
    base = dict(
        task_id="t", source="cli", raw_request="add a flag",
        spec=Spec(goal="add --verbose", acceptance_criteria=["prints debug"]),
    )
    base.update(over)
    return StromboliState(**base)  # type: ignore[arg-type]


def test_stub_when_unwired() -> None:
    out = make_coding()(_state())
    assert out["status"] == "coding"
    assert out["session_id"] == "stub-session"


def test_known_good_spec_produces_passing_diff() -> None:
    coder = FakeCoder(_run("success"))
    sandbox = FakeSandbox(SandboxResult(passed=True, output="1 passed", exit_code=0))
    node = make_coding(coder, sandbox, lambda _s: make_worktree())
    out = node(_state())
    assert out["code_diff"] == "diff --git a/x b/x\n+ok"
    assert out["inner_iterations"] == 3
    assert out["session_id"] == "sess-1"
    results = out["test_results"]
    assert isinstance(results, list) and results[0].passed is True


def test_impossible_spec_budget_exit_captures_failure() -> None:
    # error_max_turns is a clean budget exit; the sandbox reports the failure.
    coder = FakeCoder(_run("error_max_turns"))
    sandbox = FakeSandbox(SandboxResult(passed=False, output="1 failed", exit_code=1))
    node = make_coding(coder, sandbox, lambda _s: make_worktree())
    out = node(_state())
    results = out["test_results"]
    assert isinstance(results, list) and results[0].passed is False
    assert "failed" in results[0].summary


def test_non_clean_subtype_raises_node_failure() -> None:
    coder = FakeCoder(_run("error_during_execution", is_error=True))
    sandbox = FakeSandbox(SandboxResult(passed=True, output="", exit_code=0))
    node = make_coding(coder, sandbox, lambda _s: make_worktree())
    with pytest.raises(CoderError):
        node(_state())


def test_revise_pass_resumes_session_and_injects_reason() -> None:
    coder = FakeCoder(_run("success"))
    sandbox = FakeSandbox(SandboxResult(passed=True, output="ok", exit_code=0))
    node = make_coding(coder, sandbox, lambda _s: make_worktree())
    state = _state(
        session_id="sess-prev",
        verdict=Verdict(decision="revise", reason="missed acceptance criterion 2"),
    )
    node(state)
    prompt, resume = coder.calls[0]
    assert resume == "sess-prev"
    assert "missed acceptance criterion 2" in prompt


class RateLimitedCoder:
    def run(self, prompt: str, cwd: object, *, resume: str | None = None) -> CoderRun:
        raise RateLimitError(session_id="sess-live", resets_at="2026-07-01T00:00:00Z")


def test_rate_limit_escalates_and_preserves_session() -> None:
    sandbox = FakeSandbox(SandboxResult(passed=True, output="", exit_code=0))
    node = make_coding(RateLimitedCoder(), sandbox, lambda _s: make_worktree())
    out = node(_state())
    # Retryable escalation (PRD §4a): escalate, keep the session for resume.
    assert out["status"] == "escalated"
    assert out["session_id"] == "sess-live"
    reflections = out["reflections"]
    assert isinstance(reflections, list) and "rate-limited" in reflections[0]


def test_build_prompt_includes_spec_sections() -> None:
    prompt = _build_prompt(_state())
    assert "# Goal" in prompt
    assert "add --verbose" in prompt
    assert "prints debug" in prompt
