"""Tests for the verifier prompt optimizer scaffold (self-improving §2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stromboli.nodes.verifier import DEFAULT_VERIFIER_SYSTEM, verifier_predictor
from stromboli.observability.evals.verifier_optimize import (
    evaluate_prompt,
    select_best_prompt,
)


class _PromptAwareGateway:
    """Returns a decision that depends on the system prompt, so different
    prompts score differently against the dataset."""

    def __init__(self, good_prompt: str) -> None:
        self._good = good_prompt
        self.last_usage: dict[str, Any] | None = None

    def structured(self, *, model: str, system: str, user: str, schema: Any) -> Any:
        # The "good" prompt returns pass; anything else returns revise.
        decision = "pass" if system == self._good else "revise"
        return schema.model_validate({"decision": decision, "reason": "r"})


def _one_case_dataset(tmp_path: Path, decision: str = "pass") -> Path:
    import json
    p = tmp_path / "vset.json"
    p.write_text(json.dumps({
        "name": "t", "metric": "agreement", "threshold": 0.5,
        "cases": [{"id": "c1", "inputs": {"goal": "g", "acceptance_criteria": ["a"],
                    "diff": "+x", "tests_passed": True, "test_summary": "ok"},
                   "expected": {"decision": decision}}],
    }))
    return p


def test_verifier_predictor_adapts_case_inputs() -> None:
    gw = _PromptAwareGateway(DEFAULT_VERIFIER_SYSTEM)
    predict = verifier_predictor(gw, model="gemini")  # type: ignore[arg-type]
    decision = predict({"goal": "g", "acceptance_criteria": ["a"], "diff": "+x",
                        "tests_passed": True, "test_summary": "ok"})
    assert decision == "pass"


def test_evaluate_prompt_scores_against_labels(tmp_path: Path) -> None:
    gw = _PromptAwareGateway(DEFAULT_VERIFIER_SYSTEM)
    ds = _one_case_dataset(tmp_path, decision="pass")
    good = evaluate_prompt(gw, "gemini", DEFAULT_VERIFIER_SYSTEM, dataset_path=ds)
    bad = evaluate_prompt(gw, "gemini", "some other prompt", dataset_path=ds)
    assert good == 1.0 and bad == 0.0


def test_select_best_prompt_prefers_higher_agreement(tmp_path: Path) -> None:
    candidate = "the better judge prompt"
    gw = _PromptAwareGateway(candidate)  # candidate returns pass, baseline revise
    ds = _one_case_dataset(tmp_path, decision="pass")
    best = select_best_prompt(gw, "gemini", [candidate], dataset_path=ds)
    assert best.prompt == candidate and best.score == 1.0


def test_select_best_prompt_keeps_baseline_on_tie(tmp_path: Path) -> None:
    # Neither beats the baseline → baseline retained (no lateral swap).
    gw = _PromptAwareGateway(DEFAULT_VERIFIER_SYSTEM)
    ds = _one_case_dataset(tmp_path, decision="pass")
    best = select_best_prompt(gw, "gemini", ["weaker prompt"], dataset_path=ds)
    assert best.prompt == DEFAULT_VERIFIER_SYSTEM
