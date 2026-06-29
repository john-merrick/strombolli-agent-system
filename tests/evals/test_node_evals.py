"""Phase 5 — per-node evals run from datasets, post scores, and gate CI."""

from __future__ import annotations

from typing import Any

from stromboli.observability.evals import (
    gate,
    run_coding_eval,
    run_spec_eval,
    run_verifier_eval,
)
from stromboli.observability.evals.harness import EvalReport


# --- baseline (good) predictors ------------------------------------------- #
def _good_ambiguity(raw_request: str) -> bool:
    vague = {"make the app better", "improve performance somewhere", "handle the edge cases"}
    return raw_request.strip().lower().rstrip(".") in vague


def _good_decision(inputs: dict[str, Any]) -> str:
    if "TODO: cannot verify" in inputs["diff"]:
        return "escalate"
    if not inputs["tests_passed"]:
        return "revise"
    # Green tests but the diff doesn't address the criteria → revise.
    weak = ("pass", "items[start:start+size]")
    if any(w in inputs["diff"] for w in weak):
        return "revise"
    return "pass"


def _good_pass(inputs: dict[str, Any]) -> bool:
    return "Collatz" not in inputs["goal"]


def test_spec_eval_passes_with_good_predictor() -> None:
    report = run_spec_eval(_good_ambiguity)
    assert report.name == "spec_eval"
    assert report.score == 1.0
    assert report.passed


def test_verifier_eval_passes_with_good_predictor() -> None:
    report = run_verifier_eval(_good_decision)
    assert report.score >= report.threshold
    assert report.passed


def test_coding_eval_passes_with_good_predictor() -> None:
    report = run_coding_eval(_good_pass)
    assert report.passed


def test_scores_are_posted_to_sink() -> None:
    posted: list[tuple[str, float]] = []
    run_spec_eval(_good_ambiguity, sink=lambda n, v: posted.append((n, v)))
    assert posted == [("spec_eval", 1.0)]


def test_ci_gate_passes_when_all_meet_threshold() -> None:
    reports = [
        run_spec_eval(_good_ambiguity),
        run_verifier_eval(_good_decision),
        run_coding_eval(_good_pass),
    ]
    assert gate(reports) is True


def test_ci_gate_fails_on_seeded_regression() -> None:
    # A regressed verifier that rubber-stamps everything as pass.
    regressed = run_verifier_eval(lambda _inputs: "pass")
    assert regressed.passed is False
    assert gate([run_spec_eval(_good_ambiguity), regressed]) is False


def test_report_threshold_semantics() -> None:
    report = EvalReport(
        name="x", metric="m", score=0.6, threshold=0.7, results=[]
    )
    assert report.passed is False
