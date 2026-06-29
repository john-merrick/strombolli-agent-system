"""Intake node (PRD §6.1) — normalize a source payload into initial state.

For the ``cli`` source the state is already well-formed (the CLI builds it), so
intake is a pass-through that stamps ``status``. For ``notion`` it will hydrate
``raw_request`` from the task page (wired in Phase 1 / §6). Output: a well-formed
initial state with ``status="intake"``.
"""

from __future__ import annotations

from collections.abc import Callable

from stromboli.state import StromboliState

#: A LangGraph node: maps the current state to a partial-state update.
Node = Callable[[StromboliState], dict[str, object]]


def make_intake() -> Node:
    """Build the intake node."""

    def intake(state: StromboliState) -> dict[str, object]:
        # The CLI already populated task_id / source / raw_request; ensure the
        # status reflects that intake ran. Notion hydration arrives in Phase 1.
        return {"status": "intake"}

    return intake


__all__ = ["Node", "make_intake"]
