"""Memory Write node (PRD §6.9) — the terminal pre-Done learning step.

Real behavior (Phase 4): on a verified pass, deposit one episodic trace and
(maybe) one procedural skill; on a non-pass, write only the failure reflection
to episodic memory so the next similar task retrieves it at the Spec stage.

Phase 0 stub: no writes — just stamp the task ``done``.
"""

from __future__ import annotations

from stromboli.nodes.intake import Node
from stromboli.state import StromboliState


def make_memory_write() -> Node:
    """Build the memory-write node (Phase 0 stub; Chroma writes land in Phase 4)."""

    def memory_write(state: StromboliState) -> dict[str, object]:
        return {"status": "done"}

    return memory_write


__all__ = ["make_memory_write"]
