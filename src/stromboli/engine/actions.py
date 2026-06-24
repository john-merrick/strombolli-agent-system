"""The action union the control policy emits and the orchestrator dispatches.

Each :class:`~stromboli.engine.policy.ControlPolicy` ``decide`` call returns
exactly one of these immutable commands; the orchestrator runs the matching node
(or terminates). Keeping actions as data — not behaviour — is what lets the
policy be a pure, exhaustively-testable function and lets a future supervisor
policy drive the very same nodes (design decision D7).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReviewReason(StrEnum):
    """Why a build terminates in Review rather than Integrate."""

    #: One or more units were given up on (D10: never ship a partial build).
    BLOCKED_UNITS = "blocked_units"
    #: The circuit breaker pre-empted the build (cost / iteration ceiling).
    BREAKER = "breaker"
    #: A unit stalled — repeated identical diffs / unchanged verdict reasons.
    NO_PROGRESS = "no_progress"
    #: The re-plan counter cap was hit; further replanning is refused.
    REPLAN_EXHAUSTED = "replan_exhausted"
    #: The whole-diff verification rejected an otherwise all-verified build.
    WHOLE_DIFF_REJECTED = "whole_diff_rejected"


@dataclass(frozen=True)
class RunPlanner:
    """Run the planner to (re)build the plan."""


@dataclass(frozen=True)
class RunWorker:
    """Run the worker on ``unit_id`` to write code for that unit."""

    unit_id: str


@dataclass(frozen=True)
class RunVerifier:
    """Run the verifier over ``scope`` (a unit id or :data:`WHOLE_DIFF`)."""

    scope: str


@dataclass(frozen=True)
class Reflect:
    """Run the reflector on ``unit_id`` after a rejected verdict."""

    unit_id: str


@dataclass(frozen=True)
class Replan:
    """Re-run the planner because a reflector requested a structural change."""

    reason: str


@dataclass(frozen=True)
class Integrate:
    """Terminal success: open the PR and hand off to integration."""


@dataclass(frozen=True)
class RouteToReview:
    """Terminal: park the build in Review for a human, with a reason."""

    reason: ReviewReason


#: The closed set of actions a policy may emit.
Action = (
    RunPlanner
    | RunWorker
    | RunVerifier
    | Reflect
    | Replan
    | Integrate
    | RouteToReview
)


__all__ = [
    "Action",
    "Integrate",
    "Reflect",
    "Replan",
    "ReviewReason",
    "RouteToReview",
    "RunPlanner",
    "RunVerifier",
    "RunWorker",
]
