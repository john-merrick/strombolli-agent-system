"""Orchestrator-agnostic adapters over the Stromboli triage phases.

The LangGraph ``StateGraph`` remains the production engine. These adapters expose
each phase as a plain ``state -> state`` step + routing helpers, so an *external*
orchestrator (Prefect, Windmill, Temporal, …) can compose the same triage flow in
its own idiom — and visualize it in its own dashboard — without re-implementing
the node internals. Both ``prefect_flow`` and the ``windmill/`` flow call these.
"""

from stromboli.orchestration.phases import TriagePhases

__all__ = ["TriagePhases"]
