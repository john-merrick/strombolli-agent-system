"""The reflector node: diagnose a rejected unit → feedback or a replan signal.

After the verifier rejects a unit, the reflector reads the criterion, the
rejection reasons, and the diff, and decides between two outcomes encoded in a
:class:`~stromboli.engine.nodes.schemas.FeedbackModel`:

* **feedback** — actionable guidance for the worker's next attempt (the common
  case); or
* **replan** — the unit as specified is wrong/too coarse, so the build should
  re-plan (``replan=true`` with a ``replan_reason``).

This node returns a small :class:`Reflection` the orchestrator applies: append
feedback to the state, or set ``pending_replan`` so the policy issues a Replan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stromboli.engine.nodes.base import AgentNode
from stromboli.engine.nodes.schemas import FeedbackModel
from stromboli.engine.state import BuildState
from stromboli.loop import CCRunner


@dataclass(frozen=True)
class ReflectTarget:
    """What the reflector diagnoses: the rejected unit, its reasons, its diff."""

    unit_id: str
    acceptance_criterion: str
    reasons: tuple[str, ...]
    diff: str


@dataclass(frozen=True)
class Reflection:
    """The reflector's decision, normalized for the orchestrator to apply."""

    feedback: str
    replan: bool
    replan_reason: str

    @property
    def wants_replan(self) -> bool:
        """Whether this reflection asks the build to re-plan."""
        return self.replan


class ReflectorNode(AgentNode[ReflectTarget, FeedbackModel]):
    """Turns a rejected verdict into worker feedback or a replan request."""

    def __init__(self, run: CCRunner) -> None:
        super().__init__(run, FeedbackModel, name="reflector")

    def build_prompt(self, state: BuildState, target: ReflectTarget) -> str:
        reasons = "\n".join(f"- {r}" for r in target.reasons) or "- (none given)"
        return (
            "You are the reflector. The verifier REJECTED this unit. Diagnose "
            "why and produce concrete feedback for the worker's next attempt. "
            "If the unit itself is mis-specified or too coarse to ever pass, "
            "set replan=true with a replan_reason instead.\n\n"
            f"## Unit\n{target.unit_id}: {target.acceptance_criterion}\n"
            f"## Rejection reasons\n{reasons}\n"
            f"## Diff\n{target.diff}\n\n"
            "Reply with ONLY a JSON object: "
            '{"feedback": string, "replan": bool, "replan_reason": string}.'
        )

    def reflect(
        self, state: BuildState, target: ReflectTarget, cwd: Path
    ) -> Reflection:
        """Invoke the reflector and return a normalized :class:`Reflection`."""
        model = self.invoke(state, target, cwd)
        return Reflection(
            feedback=model.feedback,
            replan=model.replan,
            replan_reason=model.replan_reason,
        )


__all__ = ["ReflectTarget", "Reflection", "ReflectorNode"]
