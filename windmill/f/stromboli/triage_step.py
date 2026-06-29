"""Windmill script — run one Stromboli triage phase (orchestrator comparison B).

One parameterized step the flow calls per phase, so each node in the Windmill
flow graph is a real Stromboli phase. State crosses step boundaries as JSON
(Windmill's contract), so we (de)serialize :class:`StromboliState` here.

Deploy note: the Windmill worker must have ``stromboli`` installed and the same
env as ``.env`` (NOTION_TOKEN, LITELLM_*, etc.) — see windmill/README.md.
"""

from stromboli.orchestration.phases import TriagePhases
from stromboli.state import StromboliState

_PHASES: TriagePhases | None = None


def _phases() -> TriagePhases:
    global _PHASES
    if _PHASES is None:
        _PHASES = TriagePhases.from_settings()
    return _PHASES


def main(phase: str, state: dict) -> dict:
    """Run ``phase`` against ``state`` (a serialized StromboliState) → next state."""
    phases = _phases()
    step = {
        "intake": phases.intake,
        "spec": phases.spec,
        "prompt": phases.prompt,
        "coding": phases.coding,
        "verify": phases.verify,
        "pr": phases.open_pr,
        "memory": phases.memory_write,
        "finalize": phases.finalize,
    }[phase]
    return step(StromboliState.model_validate(state)).model_dump()
