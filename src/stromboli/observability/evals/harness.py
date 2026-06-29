"""Per-node eval harness (PRD §8) — datasets → score → Langfuse → CI gate.

A small, shared harness the three node evals build on. Each eval loads a labelled
dataset from ``evals/datasets/*.json``, runs an injected ``predict`` over the
cases, scores agreement against the labels, posts the score (best-effort) to a
:data:`ScoreSink`, and reports pass/fail against the dataset's threshold. CI
gates on :func:`gate` — any node eval below threshold fails the build.

``predict`` is injected so the gate logic is unit-tested with deterministic
predictors (no LLM); production wires the real nodes behind the same signature.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Repo-root ``evals/datasets`` directory (…/src/stromboli/observability/evals).
DATASETS_DIR = Path(__file__).resolve().parents[4] / "evals" / "datasets"

#: Receives one (score_name, value) pair — e.g. a Langfuse score writer.
ScoreSink = Callable[[str, float], None]


@dataclass(frozen=True)
class EvalCase:
    """One labelled dataset case."""

    id: str
    inputs: dict[str, Any]
    expected: dict[str, Any]


@dataclass(frozen=True)
class Dataset:
    """A loaded eval dataset."""

    name: str
    metric: str
    threshold: float
    cases: list[EvalCase]


@dataclass(frozen=True)
class CaseResult:
    """The outcome of scoring one case."""

    id: str
    correct: bool
    predicted: Any
    expected: Any


@dataclass(frozen=True)
class EvalReport:
    """A node eval's aggregate result."""

    name: str
    metric: str
    score: float
    threshold: float
    results: list[CaseResult]

    @property
    def passed(self) -> bool:
        """Whether the score meets the dataset's threshold (the CI gate)."""
        return self.score >= self.threshold


def load_dataset(path: str | Path) -> Dataset:
    """Load a dataset JSON file into a :class:`Dataset`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [
        EvalCase(id=str(c["id"]), inputs=dict(c["inputs"]), expected=dict(c["expected"]))
        for c in data["cases"]
    ]
    return Dataset(
        name=str(data["name"]),
        metric=str(data.get("metric", "accuracy")),
        threshold=float(data.get("threshold", 0.0)),
        cases=cases,
    )


def evaluate(
    dataset: Dataset,
    predict: Callable[[EvalCase], Any],
    correct: Callable[[Any, dict[str, Any]], bool],
    *,
    sink: ScoreSink | None = None,
) -> EvalReport:
    """Run ``predict`` over every case, score agreement, and (optionally) post it."""
    results: list[CaseResult] = []
    for case in dataset.cases:
        predicted = predict(case)
        results.append(
            CaseResult(
                id=case.id,
                correct=correct(predicted, case.expected),
                predicted=predicted,
                expected=case.expected,
            )
        )
    score = (
        sum(1 for r in results if r.correct) / len(results) if results else 0.0
    )
    report = EvalReport(
        name=dataset.name,
        metric=dataset.metric,
        score=score,
        threshold=dataset.threshold,
        results=results,
    )
    if sink is not None:
        post_report(report, sink)
    return report


def post_report(report: EvalReport, sink: ScoreSink) -> None:
    """Post a report's score to ``sink`` — best-effort, never raises."""
    try:
        sink(report.name, report.score)
    except Exception as exc:  # noqa: BLE001 - score posting must never fail a run
        logger.warning("Posting eval score for %s failed: %s", report.name, exc)


def gate(reports: Iterable[EvalReport]) -> bool:
    """True iff every node eval meets its threshold (the CI gate)."""
    return all(r.passed for r in reports)


def langfuse_sink(*, public_key: str, secret_key: str, host: str) -> ScoreSink:
    """A best-effort Langfuse score sink; degrades to a no-op on any error."""

    def sink(name: str, value: float) -> None:
        try:
            import importlib

            client = importlib.import_module("langfuse").Langfuse(
                public_key=public_key, secret_key=secret_key, host=host
            )
            client.create_score(name=name, value=value)
            client.flush()
        except Exception as exc:  # noqa: BLE001 - observability is best-effort
            logger.warning("Langfuse score %s failed: %s", name, exc)

    return sink


__all__ = [
    "DATASETS_DIR",
    "CaseResult",
    "Dataset",
    "EvalCase",
    "EvalReport",
    "ScoreSink",
    "evaluate",
    "gate",
    "langfuse_sink",
    "load_dataset",
    "post_report",
]
