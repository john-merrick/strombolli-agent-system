"""Tests for the typed graph state and its nested structured-output models."""

from __future__ import annotations

import operator
from typing import Any, get_type_hints

from stromboli.state import Spec, StromboliState, TestResult, Verdict


def test_initial_state_defaults() -> None:
    state = StromboliState(task_id="t1", source="cli", raw_request="do a thing")
    assert state.status == "intake"
    assert state.test_results == []
    assert state.reflections == []
    assert state.inner_iterations == 0
    assert state.outer_iterations == 0
    assert state.spec is None


def test_append_only_fields_carry_add_reducer() -> None:
    # LangGraph reads the reducer from the Annotated metadata; assert it is there.
    hints = get_type_hints(StromboliState, include_extras=True)
    for field in ("test_results", "reflections"):
        annotated: Any = hints[field]
        assert operator.add in annotated.__metadata__


def test_spec_structured_output_roundtrip() -> None:
    spec = Spec.model_validate(
        {
            "goal": "add a flag",
            "acceptance_criteria": ["flag toggles X"],
            "affected_paths": ["src/a.py"],
            "constraints": [],
            "ambiguous": False,
        }
    )
    assert spec.goal == "add a flag"
    assert spec.ambiguous is False


def test_verdict_decision_literal() -> None:
    v = Verdict(decision="revise", reason="missed AC-2", coverage_note="weak")
    assert v.decision == "revise"


def test_test_result_defaults_raw_empty() -> None:
    tr = TestResult(passed=True, summary="ok")
    assert tr.raw == ""
