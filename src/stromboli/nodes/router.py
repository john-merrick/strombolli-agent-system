"""The two conditional edges (PRD §6.3 Router, §6.6 Verdict gate).

These are plain routing functions — pure functions of state returning the name
of the next node — registered with ``add_conditional_edges``. Keeping them here
(not as nodes) makes the control flow explicit and unit-testable in isolation.
"""

from __future__ import annotations

from collections.abc import Callable

from stromboli.config import Budgets
from stromboli.state import StromboliState

#: Edge targets — node names in the compiled graph.
CODING = "coding"
HUMAN = "human"
PR = "pr"

#: A routing function: state → next node name.
RouteFn = Callable[[StromboliState], str]


def route_after_spec(state: StromboliState) -> str:
    """Router (§6.3): ambiguous spec → Human Interrupt, else → Coding."""
    if state.spec is not None and state.spec.ambiguous:
        return HUMAN
    return CODING


def make_route_after_verdict(budgets: Budgets) -> RouteFn:
    """Verdict gate (§6.6): pass → PR, revise (under cap) → Coding, else → Human.

    ``escalate`` or an exhausted revision budget routes to the Human Interrupt.
    The revise budget is the **outer** recursion bound (PRD §5).
    """

    def route_after_verdict(state: StromboliState) -> str:
        verdict = state.verdict
        if verdict is None:  # defensive — verifier always sets one
            return HUMAN
        if verdict.decision == "pass":
            return PR
        if (
            verdict.decision == "revise"
            and state.outer_iterations < budgets.max_outer_revisions
        ):
            return CODING
        # escalate, or revise budget exhausted
        return HUMAN

    return route_after_verdict


__all__ = [
    "CODING",
    "HUMAN",
    "PR",
    "RouteFn",
    "make_route_after_verdict",
    "route_after_spec",
]
