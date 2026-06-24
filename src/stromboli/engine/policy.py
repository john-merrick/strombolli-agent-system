"""The control policy — the deterministic brain of the graph engine.

A :class:`ControlPolicy` maps the current :class:`~stromboli.engine.state.BuildState`
to the next :class:`~stromboli.engine.actions.Action`. It is the single place the
build's control flow lives, and it is **pure**: no I/O, no mutation — given the
same state it always returns the same action. That purity is what makes the
whole correctness story testable before a single agent exists, and what lets a
future supervisor/LLM policy be swapped in behind the same ``decide`` seam (D7).

:class:`DeterministicPolicy` implements the v1 rules. In precedence order:

1. **breaker pre-empt** → Review (the build is over budget; nothing else matters);
2. **no plan** → run the planner;
3. **pending replan** (a reflector asked to restructure) → Replan while the
   re-plan counter is under cap, else Review;
4. an **eligible unit** → advance its work → verify → reflect micro-cycle,
   bailing to Review on attempt-exhaustion (K) or a no-progress stall;
5. **any blocked unit** → Review (D10: never ship a partial build); else
6. **all verified** → verify the whole diff, then Integrate (or Review if the
   whole-diff check rejects).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from stromboli.engine.actions import (
    Action,
    Integrate,
    Reflect,
    Replan,
    ReviewReason,
    RouteToReview,
    RunPlanner,
    RunVerifier,
    RunWorker,
)
from stromboli.engine.state import (
    WHOLE_DIFF,
    BuildState,
    Unit,
    UnitPhase,
)

#: Default per-unit attempt ceiling (K).
DEFAULT_MAX_ATTEMPTS = 3
#: Default re-plan counter cap.
DEFAULT_MAX_REPLANS = 2


class ControlPolicy(Protocol):
    """Maps a :class:`BuildState` to the next :class:`Action` (pure)."""

    def decide(self, state: BuildState) -> Action: ...


@dataclass(frozen=True)
class DeterministicPolicy:
    """The v1 rule-based policy — pure and exhaustively branch-testable."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_replans: int = DEFAULT_MAX_REPLANS

    def decide(self, state: BuildState) -> Action:
        """Pick the next action for ``state`` (see the module docstring rules)."""
        # 1. The breaker pre-empts everything: the build is over budget.
        if state.breaker_tripped:
            return RouteToReview(ReviewReason.BREAKER)

        # 2. No plan yet → plan.
        if state.plan.is_empty:
            return RunPlanner()

        # 3. A reflector asked to restructure: replan while under the cap.
        if state.pending_replan is not None:
            if state.replan_count < self.max_replans:
                return Replan(state.pending_replan)
            return RouteToReview(ReviewReason.REPLAN_EXHAUSTED)

        # 4. Work the next eligible unit.
        unit = state.next_eligible_unit()
        if unit is not None:
            return self._decide_unit(unit)

        # 5. No eligible unit: any blocked one parks the whole build (D10).
        if state.blocked_units():
            return RouteToReview(ReviewReason.BLOCKED_UNITS)

        # 6. Every unit verified → whole-diff verification, then integrate.
        return self._decide_whole(state)

    def _decide_unit(self, unit: Unit) -> Action:
        """The micro-cycle for one PENDING unit, with the bail-out guards."""
        # A stalled unit (identical diffs across attempts) is hopeless.
        if self._no_progress(unit):
            return RouteToReview(ReviewReason.NO_PROGRESS)
        # Attempts exhausted: this unit is effectively blocked → Review (D10).
        if unit.attempts >= self.max_attempts:
            return RouteToReview(ReviewReason.BLOCKED_UNITS)
        # Otherwise advance by phase.
        if unit.phase is UnitPhase.WORK:
            return RunWorker(unit.id)
        if unit.phase is UnitPhase.VERIFY:
            return RunVerifier(unit.id)
        return Reflect(unit.id)  # UnitPhase.REFLECT

    def _decide_whole(self, state: BuildState) -> Action:
        """All units verified: gate the whole diff before integrating."""
        if state.whole_verdict is None:
            return RunVerifier(WHOLE_DIFF)
        if state.whole_verdict.met:
            return Integrate()
        return RouteToReview(ReviewReason.WHOLE_DIFF_REJECTED)

    @staticmethod
    def _no_progress(unit: Unit) -> bool:
        """Detect a stall: the last two worker attempts produced no change.

        Two consecutive identical diff hashes mean the worker is spinning — the
        same code being rewritten without moving the verdict — so the unit is
        routed to Review rather than burning the rest of its attempts.
        """
        hashes = unit.diff_hashes
        if len(hashes) >= 2 and hashes[-1] == hashes[-2]:
            return True
        # Same rejection reasons twice running with no new diff is also a stall.
        return False


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_REPLANS",
    "ControlPolicy",
    "DeterministicPolicy",
]
