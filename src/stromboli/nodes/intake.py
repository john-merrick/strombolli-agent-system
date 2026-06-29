"""Intake node (PRD §6.1) — normalize a source payload into initial state.

For ``cli`` / ``telegram`` the state is already well-formed (the caller built it),
so intake stamps ``status``. For ``notion`` — the front-end for adding tasks —
it hydrates ``raw_request`` from the task page (the task's Spec, falling back to
its name) given a Notion reader. Output: a well-formed initial state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from stromboli.integrations.notion import STATUS_WORKING, Task
from stromboli.state import StromboliState

logger = logging.getLogger(__name__)

#: A LangGraph node: maps the current state to a partial-state update.
Node = Callable[[StromboliState], dict[str, object]]


class NotionReader(Protocol):
    """The slice of the Notion client intake needs (read one task + set status)."""

    def get_task(self, page_id: str) -> Task: ...

    def update_task(self, page_id: str, *, status: str | None = ...) -> None: ...


def make_intake(notion: NotionReader | None = None) -> Node:
    """Build the intake node. ``notion`` enables hydrating a Notion task and
    moving it To do → Working on the moment Stromboli picks it up."""

    def intake(state: StromboliState) -> dict[str, object]:
        if state.source == "notion" and notion is not None:
            task = notion.get_task(state.task_id)
            raw = task.spec.strip() or task.name
            # Reflect "picked up" on the board (best-effort).
            try:
                notion.update_task(state.task_id, status=STATUS_WORKING)
            except Exception as exc:  # noqa: BLE001 - status write must not crash
                logger.warning("Could not set Working on for %s: %s",
                               state.task_id, exc)
            logger.info("Hydrated Notion task %s for intake.", state.task_id)
            return {"raw_request": raw, "status": "intake"}
        # CLI / telegram already populated raw_request.
        return {"status": "intake"}

    return intake


__all__ = ["Node", "NotionReader", "make_intake"]
