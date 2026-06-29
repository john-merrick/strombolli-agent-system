"""spec_eval (PRD §8) — Spec node ambiguity-detection accuracy.

Scores how often the Spec node correctly flags a request as ambiguous vs ready,
against the labelled :file:`evals/datasets/spec_eval.json`. ``predict_ambiguous``
is injected (in production: the real Spec node behind the gateway).
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

DEFAULT_DATASET = DATASETS_DIR / "spec_eval.json"

#: Predicts whether a raw request is ambiguous.
AmbiguityPredictor = Callable[[str], bool]


def run_spec_eval(
    predict_ambiguous: AmbiguityPredictor,
    *,
    dataset_path: str | Path = DEFAULT_DATASET,
    sink: ScoreSink | None = None,
) -> EvalReport:
    """Evaluate ambiguity detection over the spec dataset."""
    dataset = load_dataset(dataset_path)

    def predict(case: EvalCase) -> bool:
        return predict_ambiguous(str(case.inputs["raw_request"]))

    def correct(predicted: Any, expected: dict[str, Any]) -> bool:
        return bool(predicted) == bool(expected["ambiguous"])

    return evaluate(dataset, predict, correct, sink=sink)


__all__ = ["DEFAULT_DATASET", "AmbiguityPredictor", "run_spec_eval"]
