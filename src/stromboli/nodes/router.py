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
VERIFIER = "verifier"
PROMPT = "prompt"
MEMORY = "memory"

#: A routing function: state → next node name.
RouteFn = Callable[[StromboliState], str]


def route_after_spec(state: StromboliState) -> str:
    """Router (§6.3): ambiguous spec → Human Interrupt, else → the Prompt node.

    A ready spec goes to the prompt agent (Spec→Prompt→Coding); only the build
    path generates a prompt, so ambiguous specs skip it.
    """
    if state.spec is not None and state.spec.ambiguous:
        return HUMAN
    return PROMPT


def route_after_coding(state: StromboliState) -> str:
    """After coding: a rate-limit escalation → Human, otherwise → Verifier.

    The coding node sets ``status="escalated"`` on a rate-limit cutoff (PRD §4a),
    a retryable escalation; every other outcome proceeds to verification.
    """
    if state.status == "escalated":
        return HUMAN
    return VERIFIER


def route_after_pr(state: StromboliState) -> str:
    """After PR: a publication failure → Human, otherwise → Memory Write.

    The PR node sets ``status="escalated"`` when the push / API call fails
    (an operational fault after a verified diff) — that needs a human, not a
    memory write that would overwrite the status with ``done``.
    """
    if state.status == "escalated":
        return HUMAN
    return MEMORY


def make_route_after_verdict(budgets: Budgets) -> RouteFn:
    """Verdict gate (§6.6): pass → PR, revise (under cap) → Coding, else → Human.

    ``escalate`` or an exhausted budget routes to the Human Interrupt. Two
    budgets gate a revise cycle: the revise cap (the **outer** recursion bound,
    PRD §5) and the per-task token ceiling ``max_tokens_per_task`` — once the
    task has spent past it, another coding pass is refused (backstop, §4a).
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
            and state.tokens_used < budgets.max_tokens_per_task
        ):
            return CODING
        # escalate, or a budget (revise cap / token ceiling) exhausted
        return HUMAN

    return route_after_verdict


__all__ = [
    "CODING",
    "HUMAN",
    "MEMORY",
    "PR",
    "PROMPT",
    "VERIFIER",
    "RouteFn",
    "make_route_after_verdict",
    "route_after_coding",
    "route_after_pr",
    "route_after_spec",
]
