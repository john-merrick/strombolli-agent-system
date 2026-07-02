"""Skill gate (self-improving §3) — promote a candidate only if it doesn't regress.

A distilled skill enters procedural memory as an unvetted *candidate* and is
never injected into the coder until this gate promotes it. The gate A/Bs the
candidate over the coding eval: it runs the coder pass-rate **baseline**
(skills off) and **with the candidate injected**, and promotes only when the
candidate does not lower the pass-rate. This is the "a bad skill can't silently
degrade the system" rail.

``predict_pass`` is injected (production: run the coder + sandbox and report
whether the final test run passed) with a flag for whether the candidate skill
is active, so the same harness scores both arms without touching the graph.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from stromboli.observability.evals.coding_eval import run_coding_eval
from stromboli.observability.evals.harness import ScoreSink

#: Runs the coder over one eval case's inputs with the candidate skill on/off,
#: returning whether the final test run passed.
SkillAwarePredictor = Callable[[dict[str, Any], bool], bool]


@dataclass(frozen=True)
class GateResult:
    """The outcome of a skill A/B gate."""

    key: str
    baseline_score: float
    candidate_score: float
    promoted: bool


def gate_skill(
    procedural: object,
    key: str,
    predict_pass: SkillAwarePredictor,
    *,
    now: Callable[[], float] = time.time,
    sink: ScoreSink | None = None,
) -> GateResult:
    """Evaluate a candidate skill A/B; promote it iff it doesn't regress pass-rate.

    ``procedural`` is a :class:`ProceduralMemory`. Promotion is a no-regression
    check (candidate >= baseline), so a neutral skill is allowed through while a
    harmful one is rejected and left as a candidate (never injected).
    """
    baseline = run_coding_eval(lambda inp: predict_pass(inp, False), sink=sink)
    candidate = run_coding_eval(lambda inp: predict_pass(inp, True), sink=sink)
    promoted = candidate.score >= baseline.score
    if promoted:
        procedural.promote(key, ts=now())  # type: ignore[attr-defined]
    return GateResult(
        key=key,
        baseline_score=baseline.score,
        candidate_score=candidate.score,
        promoted=promoted,
    )


__all__ = ["GateResult", "SkillAwarePredictor", "gate_skill"]
