"""Per-node eval datasets + scoring + CI gate (PRD §8).

Three node evals — ``spec_eval`` (ambiguity detection), ``coding_eval`` (test
pass-rate), and ``verifier_eval`` (agreement with human labels, the most
important) — each load a labelled dataset, score an injected predictor, post the
score to Langfuse, and gate CI via :func:`~harness.gate`.
"""

from stromboli.observability.evals.coding_eval import run_coding_eval
from stromboli.observability.evals.harness import (
    EvalReport,
    ScoreSink,
    gate,
    langfuse_sink,
    load_dataset,
)
from stromboli.observability.evals.spec_eval import run_spec_eval
from stromboli.observability.evals.verifier_eval import run_verifier_eval

__all__ = [
    "EvalReport",
    "ScoreSink",
    "gate",
    "langfuse_sink",
    "load_dataset",
    "run_coding_eval",
    "run_spec_eval",
    "run_verifier_eval",
]
