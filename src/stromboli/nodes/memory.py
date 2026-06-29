"""Memory Write node (PRD §6.9) — the terminal pre-Done learning step.

Write policy (PRD §7):

* persist every accumulated **reflection** to episodic memory (the loop-closer —
  the next similar task retrieves it at the Spec stage), even when the task only
  passed after revisions;
* on a **verified pass**, deposit one episodic **trace**; and
* procedural **skills** are written only on a verified pass (deferred to an
  explicit extraction step — none are auto-written in v1, PRD §11.5).

With no memory wired it is a no-op that stamps ``done`` (Phase 0 / tests).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from stromboli.memory import Memory
from stromboli.nodes.intake import Node
from stromboli.state import StromboliState


def make_memory_write(
    memory: Memory | None = None, *, now: Callable[[], float] = time.time
) -> Node:
    """Build the memory-write node. With no ``memory`` it is a no-op."""

    def memory_write(state: StromboliState) -> dict[str, object]:
        if memory is None:
            return {"status": "done"}

        ts = now()
        written: list[str] = []
        # Persist failure context (reflections) — written regardless of outcome.
        for i, reflection in enumerate(state.reflections):
            written.append(
                memory.episodic.record_reflection(
                    state.task_id, reflection, ts=ts, seq=i
                )
            )

        passed = state.verdict is not None and state.verdict.decision == "pass"
        if passed:
            goal = state.spec.goal if state.spec else state.raw_request
            summary = f"Task {state.task_id}: {goal} — shipped (PR opened)."
            written.append(memory.episodic.record_trace(state.task_id, summary, ts=ts))

        return {"status": "done", "memory_refs": [*state.memory_refs, *written]}

    return memory_write


__all__ = ["make_memory_write"]
