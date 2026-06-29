"""verifier_eval (PRD §8) — the most important eval: agreement with humans.

Scores how often the verifier's decision (pass / revise / escalate) matches the
human label on :file:`evals/datasets/verifier_eval.json`. A bad judge poisons
everything, so this is the eval to calibrate verifier strictness against (§11.6).
``predict_decision`` is injected (in production: the real verifier on Gemini).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from stromboli.observability.evals.harness import (
    DATASETS_DIR,
    EvalCase,
    EvalReport,
    ScoreSink,
    evaluate,
    load_dataset,
)

DEFAULT_DATASET = DATASETS_DIR / "verifier_eval.json"

#: Predicts a verdict decision ("pass" / "revise" / "escalate") from case inputs.
DecisionPredictor = Callable[[dict[str, Any]], str]


def run_verifier_eval(
    predict_decision: DecisionPredictor,
    *,
    dataset_path: str | Path = DEFAULT_DATASET,
    sink: ScoreSink | None = None,
) -> EvalReport:
    """Evaluate verifier-vs-human agreement over the verifier dataset."""
    dataset = load_dataset(dataset_path)

    def predict(case: EvalCase) -> str:
        return predict_decision(case.inputs)

    def correct(predicted: Any, expected: dict[str, Any]) -> bool:
        return str(predicted) == str(expected["decision"])

    return evaluate(dataset, predict, correct, sink=sink)


__all__ = ["DEFAULT_DATASET", "DecisionPredictor", "run_verifier_eval"]
