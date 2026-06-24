"""The worker node: write code in the worktree for one unit.

Unlike the typed nodes, the worker's product is *file edits in the worktree*,
not a structured model — so it does not fail-closed on a schema. It builds a
focused prompt (the unit's criterion + definition-of-done + any reflector
feedback), invokes Claude Code in the worktree (which performs the edits), and
returns the agent's free-text summary for the transcript. The orchestrator
computes the resulting diff hash for the no-progress detector.
"""

from __future__ import annotations

from pathlib import Path

from stromboli.engine.nodes.base import result_text
from stromboli.engine.state import BuildState, Unit
from stromboli.loop import CCRunner, build_cc_argv


class WorkerNode:
    """Edits the worktree to satisfy a single unit's acceptance criterion."""

    name = "worker"

    def __init__(self, run: CCRunner) -> None:
        self._run = run

    def build_prompt(self, state: BuildState, target: Unit) -> str:
        feedback_block = (
            f"\n## Reviewer feedback to address\n{state.feedback}\n"
            if state.feedback
            else ""
        )
        return (
            "You are the worker. Implement the change in this repository so the "
            "acceptance criterion is met. Write real code and real tests — no "
            "stubs, no hollow assertions.\n\n"
            f"## Unit {target.id}: {target.description}\n"
            f"## Acceptance criterion\n{target.acceptance_criterion}\n"
            f"## Definition of done\n{target.definition_of_done}\n"
            f"{feedback_block}"
        )

    def work(self, state: BuildState, target: Unit, cwd: Path) -> str:
        """Invoke the worker in ``cwd`` and return its free-text summary."""
        stdout = self._run(build_cc_argv(self.build_prompt(state, target)), cwd)
        return result_text(stdout)


__all__ = ["WorkerNode"]
