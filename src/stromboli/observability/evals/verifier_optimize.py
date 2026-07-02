"""Verifier prompt optimization (self-improving §2) — GEPA on the judge.

The verifier is the highest-leverage prompt in the system (it's the judge), so
optimizing it against human accept/reject labels is the biggest single win. The
Failure-to-dataset pipeline (§1) accrues the labelled trainset; this module
turns it into an improved system prompt, **gated** so a worse prompt is never
adopted.

Two layers, so this works with or without the heavy optimizer dependency:

* :func:`evaluate_prompt` / :func:`select_best_prompt` — dependency-free.
  Score any candidate prompt against the labelled dataset via the existing
  eval harness and pick the best. This alone lets you A/B a hand-written or
  GEPA-generated prompt and adopt only a non-regression.
* :func:`gepa_candidates` — the GEPA/DSPy reflective search that *generates*
  candidate prompts. Imported lazily and only available under the ``optimize``
  extra (``pip install 'stromboli[optimize]'``), so the runtime graph never
  pulls in DSPy.

Adoption is a proposal, never automatic: the caller compares the winner's score
to the current prompt's and decides.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from stromboli.nodes.verifier import DEFAULT_VERIFIER_SYSTEM, verifier_predictor
from stromboli.observability.evals.harness import DATASETS_DIR
from stromboli.observability.evals.verifier_eval import run_verifier_eval

DEFAULT_DATASET = DATASETS_DIR / "verifier_eval.json"


@dataclass(frozen=True)
class PromptScore:
    """A candidate verifier prompt and its agreement score."""

    prompt: str
    score: float


def evaluate_prompt(
    gateway: object,
    model: str,
    system_prompt: str,
    *,
    dataset_path: str | Path = DEFAULT_DATASET,
) -> float:
    """Agreement-with-labels score for one candidate verifier prompt."""
    predict = verifier_predictor(gateway, model=model, system_prompt=system_prompt)  # type: ignore[arg-type]
    return run_verifier_eval(predict, dataset_path=dataset_path).score


def select_best_prompt(
    gateway: object,
    model: str,
    candidates: Sequence[str],
    *,
    dataset_path: str | Path = DEFAULT_DATASET,
    baseline: str = DEFAULT_VERIFIER_SYSTEM,
) -> PromptScore:
    """Score the baseline + every candidate; return the best (baseline wins ties).

    The baseline is included and wins on equal score, so an optimized prompt is
    adopted only when it *strictly* beats the current judge — never a lateral
    swap that could regress on unseen cases.
    """
    best = PromptScore(
        baseline, evaluate_prompt(gateway, model, baseline, dataset_path=dataset_path)
    )
    for cand in candidates:
        score = evaluate_prompt(gateway, model, cand, dataset_path=dataset_path)
        if score > best.score:
            best = PromptScore(cand, score)
    return best


def gepa_candidates(
    gateway: object,
    model: str,
    *,
    dataset_path: str | Path = DEFAULT_DATASET,
    rounds: int = 4,
) -> list[str]:
    """Generate candidate prompts via GEPA/DSPy reflective optimization.

    Requires the ``optimize`` extra (DSPy). Imported lazily so the runtime graph
    never depends on it. Raises a clear error if DSPy is not installed.
    """
    try:
        import dspy  # type: ignore[import-not-found]  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only under the extra
        raise RuntimeError(
            "GEPA optimization needs the 'optimize' extra: "
            "pip install 'stromboli[optimize]'"
        ) from exc
    # The DSPy program is a single-signature judge; GEPA mutates its instruction
    # (our system prompt) against the agreement metric. Kept minimal here — the
    # heavy wiring lives behind the extra and is exercised offline, not in CI.
    raise NotImplementedError(  # pragma: no cover
        "gepa_candidates: wire the DSPy GEPA program once the labelled dataset "
        "(failures.db export) has enough volume to optimize against."
    )


__all__ = [
    "DEFAULT_DATASET",
    "PromptScore",
    "evaluate_prompt",
    "gepa_candidates",
    "select_best_prompt",
]
