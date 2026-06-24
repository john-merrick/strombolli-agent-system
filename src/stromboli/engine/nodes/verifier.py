"""The verifier node: judge whether a diff meets a scope's criterion.

The verifier is the engine's anti-hollow-test guard. Given the acceptance
criterion and the actual diff (for one unit, or the whole accumulated diff), it
emits a :class:`~stromboli.engine.nodes.schemas.VerdictModel` — explicitly
flagging ``hollow_tests`` — which this node turns into an engine
:class:`~stromboli.engine.state.Verdict` scoped to the target. Garble never
becomes a pass: a parse failure raises (fail-closed) and the policy reflects /
blocks rather than integrating.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stromboli.engine.nodes.base import AgentNode
from stromboli.engine.nodes.schemas import VerdictModel
from stromboli.engine.state import BuildState, Verdict
from stromboli.loop import CCRunner


@dataclass(frozen=True)
class VerifyTarget:
    """What the verifier judges: a scope (unit id or whole-diff) + its diff."""

    scope: str
    acceptance_criterion: str
    diff: str
    definition_of_done: str = ""


class VerifierNode(AgentNode[VerifyTarget, VerdictModel]):
    """Judges a diff against its acceptance criterion, fail-closed."""

    def __init__(self, run: CCRunner) -> None:
        super().__init__(run, VerdictModel, name="verifier")

    def build_prompt(self, state: BuildState, target: VerifyTarget) -> str:
        dod = (
            f"## Definition of done\n{target.definition_of_done}\n"
            if target.definition_of_done
            else ""
        )
        return (
            "You are the verifier. Decide whether the diff below genuinely "
            "satisfies the acceptance criterion. Be adversarial about HOLLOW "
            "TESTS — tests that assert nothing meaningful, are skipped, or are "
            "tautological — and set hollow_tests=true if you find any.\n\n"
            f"## Scope\n{target.scope}\n"
            f"## Acceptance criterion\n{target.acceptance_criterion}\n"
            f"{dod}"
            f"## Diff\n{target.diff}\n\n"
            "Reply with ONLY a JSON object: "
            '{"met": bool, "hollow_tests": bool, "reasons": [string]}.'
        )

    def verify(self, state: BuildState, target: VerifyTarget, cwd: Path) -> Verdict:
        """Invoke the verifier and return a scoped :class:`Verdict`."""
        model = self.invoke(state, target, cwd)
        return Verdict(
            scope=target.scope,
            met=model.met,
            hollow_tests=model.hollow_tests,
            reasons=tuple(model.reasons),
        )


__all__ = ["VerifierNode", "VerifyTarget"]
