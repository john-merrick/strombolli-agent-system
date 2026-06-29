"""coding_eval (PRD §8) — Coding node test pass-rate.

Scores how often the coding node lands a passing solution on the solvable tasks
in :file:`evals/datasets/coding_eval.json` (and correctly fails to "pass" the
unsolvable one). ``predict_pass`` is injected (in production: run the coder +
sandbox and report whether the final test run passed). Iterations-to-green and
cost-per-task are recorded per case for trend tracking.
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

DEFAULT_DATASET = DATASETS_DIR / "coding_eval.json"

#: Predicts whether the coder lands a passing solution for a task's inputs.
PassPredictor = Callable[[dict[str, Any]], bool]


def run_coding_eval(
    predict_pass: PassPredictor,
    *,
    dataset_path: str | Path = DEFAULT_DATASET,
    sink: ScoreSink | None = None,
) -> EvalReport:
    """Evaluate coder pass-rate over the coding dataset."""
    dataset = load_dataset(dataset_path)

    def predict(case: EvalCase) -> bool:
        return predict_pass(case.inputs)

    def correct(predicted: Any, expected: dict[str, Any]) -> bool:
        return bool(predicted) == bool(expected["should_pass"])

    return evaluate(dataset, predict, correct, sink=sink)


__all__ = ["DEFAULT_DATASET", "PassPredictor", "run_coding_eval"]
