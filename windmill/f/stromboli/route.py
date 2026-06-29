"""Windmill script — stateless routing decision for the triage flow branches.

``kind="ambiguous"`` → ``"escalate"`` | ``"continue"`` (after spec);
``kind="verdict"``   → ``"pr"`` | ``"revise"`` | ``"escalate"`` (after verify).
The revise loop's bound is enforced by the flow's while-loop max-iterations, so
this stays stateless.
"""

from stromboli.state import StromboliState


def main(kind: str, state: dict) -> str:
    s = StromboliState.model_validate(state)
    if kind == "ambiguous":
        return "escalate" if (s.spec is not None and s.spec.ambiguous) else "continue"
    if kind == "verdict":
        if s.verdict is None:
            return "escalate"
        return {"pass": "pr", "revise": "revise", "escalate": "escalate"}.get(
            s.verdict.decision, "escalate"
        )
    return "continue"
