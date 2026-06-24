"""The planner node: decompose a task spec into a versioned :class:`Plan`.

Given the task's free-text spec (and any reflector replan feedback already on
the state), the planner emits a :class:`~stromboli.engine.nodes.schemas.PlanModel`
which this node turns into engine :class:`~stromboli.engine.state.Unit`\\ s with
stable ``AC-NNN`` ids, bumping the plan version (so a replan supersedes the old
plan).
"""

from __future__ import annotations

from pathlib import Path

from stromboli.engine.nodes.base import AgentNode
from stromboli.engine.nodes.schemas import PlanModel
from stromboli.engine.state import BuildState, Plan, Unit
from stromboli.loop import CCRunner

#: The id prefix for every generated unit.
UNIT_PREFIX = "AC"


class PlannerNode(AgentNode[str, PlanModel]):
    """Plans (or replans) the build from the task spec."""

    def __init__(self, run: CCRunner) -> None:
        super().__init__(run, PlanModel, name="planner")

    def build_prompt(self, state: BuildState, target: str) -> str:
        feedback = state.pending_replan or state.feedback
        feedback_block = (
            f"\nA previous plan was rejected. Address this feedback:\n{feedback}\n"
            if feedback
            else ""
        )
        return (
            "You are the planner for an autonomous build. Decompose the task "
            "below into the smallest set of independently-verifiable units. "
            "Each unit needs a precise acceptance_criterion and a "
            "definition_of_done.\n\n"
            f"## Task spec\n{target}\n"
            f"{feedback_block}\n"
            "Reply with ONLY a JSON object: "
            '{"units": [{"description", "acceptance_criterion", '
            '"definition_of_done"}]}.'
        )

    def plan(self, state: BuildState, spec: str, cwd: Path) -> Plan:
        """Invoke the planner and build a fresh, versioned :class:`Plan`."""
        model = self.invoke(state, spec, cwd)
        units = [
            Unit(
                id=f"{UNIT_PREFIX}-{i:03d}",
                description=u.description,
                acceptance_criterion=u.acceptance_criterion,
                definition_of_done=u.definition_of_done,
            )
            for i, u in enumerate(model.units, start=1)
        ]
        return Plan(version=state.plan.version + 1, units=units)


__all__ = ["UNIT_PREFIX", "PlannerNode"]
