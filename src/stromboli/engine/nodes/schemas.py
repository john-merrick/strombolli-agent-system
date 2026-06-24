"""Strict pydantic contracts for agent output (Phase 2).

Every agent node parses its model's JSON into one of these. They are
``extra="forbid"`` and fully typed so a malformed or hallucinated field is a
**validation error**, not a silently-dropped key — which is what lets the node
seam fail closed (re-prompt once, then raise) instead of ever auto-passing on
garble.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: Shared strict config: reject unknown keys, no coercion surprises.
_STRICT = ConfigDict(extra="forbid")


class PlanUnitModel(BaseModel):
    """One unit in a planner's decomposition."""

    model_config = _STRICT

    description: str
    acceptance_criterion: str
    definition_of_done: str = ""


class PlanModel(BaseModel):
    """The planner's output: an ordered list of units to build."""

    model_config = _STRICT

    units: list[PlanUnitModel] = Field(min_length=1)


class VerdictModel(BaseModel):
    """The verifier's judgement of a scope (a unit or the whole diff)."""

    model_config = _STRICT

    met: bool
    hollow_tests: bool = False
    reasons: list[str] = Field(default_factory=list)


class FeedbackModel(BaseModel):
    """The reflector's diagnosis: feedback for the worker, or a replan signal."""

    model_config = _STRICT

    feedback: str
    replan: bool = False
    replan_reason: str = ""


__all__ = [
    "FeedbackModel",
    "PlanModel",
    "PlanUnitModel",
    "VerdictModel",
]
