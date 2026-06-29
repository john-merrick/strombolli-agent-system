"""Tests for the Reflective Verifier node (PRD §6.5)."""

from __future__ import annotations

from stromboli.nodes.verifier import make_verifier
from stromboli.state import Spec, StromboliState, TestResult, Verdict
from tests.nodes._fakes import FakeGateway


def _state(**over: object) -> StromboliState:
    base: dict[str, object] = dict(
        task_id="t", source="cli", raw_request="add a flag",
        spec=Spec(goal="add --verbose", acceptance_criteria=["prints debug when set"]),
        code_diff="diff --git a/x b/x\n+pass",
        test_results=[TestResult(passed=True, summary="1 passed")],
    )
    base.update(over)
    return StromboliState(**base)  # type: ignore[arg-type]


def test_stub_passes_without_gateway() -> None:
    out = make_verifier()(_state())
    verdict = out["verdict"]
    assert isinstance(verdict, Verdict) and verdict.decision == "pass"


def test_weak_tests_returns_revise_with_reflection() -> None:
    # The fixture: tests are green but miss an acceptance criterion → revise.
    gw = FakeGateway(
        {"decision": "revise", "reason": "AC 'prints debug' is untested",
         "coverage_note": "tests assert nothing about the debug output"}
    )
    out = make_verifier(gw, model="gemini/gemini-2.5-pro")(_state())
    verdict = out["verdict"]
    assert isinstance(verdict, Verdict) and verdict.decision == "revise"
    reflections = out["reflections"]
    assert isinstance(reflections, list) and "untested" in reflections[0]


def test_verifier_runs_on_non_claude_model() -> None:
    gw = FakeGateway({"decision": "pass", "reason": "ok"})
    make_verifier(gw, model="gemini/gemini-2.5-pro")(_state())
    # The verifier must judge on a different family than the Claude coder.
    model = gw.calls[0]["model"]
    assert "gemini" in model
    assert "claude" not in model


def test_pass_appends_no_reflection() -> None:
    gw = FakeGateway({"decision": "pass", "reason": "meets all criteria"})
    out = make_verifier(gw, model="gemini/gemini-2.5-pro")(_state())
    assert "reflections" not in out


def test_gateway_failure_escalates() -> None:
    from stromboli.llm.gateway import GatewayError

    gw = FakeGateway(error=GatewayError("gemini down"))
    out = make_verifier(gw, model="gemini/gemini-2.5-pro")(_state())
    verdict = out["verdict"]
    assert isinstance(verdict, Verdict) and verdict.decision == "escalate"
