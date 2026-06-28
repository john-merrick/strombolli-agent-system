"""Notion acknowledgment — tell the task it was picked up, the moment it is.

The gap this closes: after ticking *Ready* you had no confirmation Stromboli
ever received the dispatch. :class:`NotionAck` is a lifecycle listener that
appends a concise note to the task as soon as it is **queued** (with its place
in line) and again when it starts **building** — so the Notion page you already
live in answers "did it kick off / is it running" without checking logs.

The *finished* outcome is intentionally a no-op here: the build pipeline already
writes a detailed feedback summary back to the task, so acking it again would
just be clutter. (A future Telegram notifier is a separate listener that *will*
care about ``finished``.) Appends are best-effort via
:func:`~stromboli.writeback.resilient_append`, so a Notion hiccup never disturbs
the build.
"""

from __future__ import annotations

from dataclasses import dataclass

from stromboli.ledger import RunRecord
from stromboli.writeback import AppendGateway, resilient_append


def render_queued(run: RunRecord, position: int) -> str:
    """The note appended when a dispatch is accepted into the queue."""
    where = "next up" if position == 0 else f"#{position + 1} in the queue"
    return f"🍕 **Stromboli queued** this task ({where}) at {run.queued_at}."


def render_building(run: RunRecord) -> str:
    """The note appended when the build actually starts."""
    return f"🛠️ **Stromboli is building** this task (run {run.id})."


@dataclass
class NotionAck:
    """Lifecycle listener that acks queued / building back to the Notion task."""

    notion: AppendGateway

    def queued(self, run: RunRecord, position: int) -> None:
        resilient_append(self.notion, run.page_id, render_queued(run, position))

    def building(self, run: RunRecord) -> None:
        resilient_append(self.notion, run.page_id, render_building(run))

    def finished(self, run: RunRecord) -> None:
        # The build pipeline already writes the outcome summary; don't double up.
        return


__all__ = [
    "NotionAck",
    "render_building",
    "render_queued",
]
