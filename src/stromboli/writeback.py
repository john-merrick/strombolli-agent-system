"""Resilient outcome write-back to Notion (US-012).

When a build finishes — cleanly, with blocked items, or on a breaker trip — the
worker writes its outcome back to the task so a supervisor can triage from
Notion without reading logs:

* :func:`build_feedback_summary` renders a markdown summary — what was done, the
  PR link, any blocked items *and why*, and a breaker-trip note when present.
* :func:`resilient_append` appends it with bounded retries: a transient Notion
  failure is retried and logged, and a permanent failure returns ``False``
  rather than raising, so a write-back problem never crashes the worker.

The final ``Status`` itself is set by routing (US-011) / the breaker (US-008);
this module adds the human-readable feedback on top.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from stromboli.breaker import BreakerTrip

logger = logging.getLogger(__name__)

#: Base seconds for exponential backoff between write-back retries.
_BACKOFF_BASE: Final = 0.5


@dataclass(frozen=True)
class BlockedItem:
    """A PRD item the loop gave up on, with its diagnosis."""

    id: str
    reason: str


class AppendGateway(Protocol):
    """The slice of the Notion client write-back needs."""

    def append_task_body(self, page_id: str, markdown: str) -> None: ...


def read_blocked_items(prd_path: str | Path) -> list[BlockedItem]:
    """Read the worktree PRD and return its blocked items with reasons."""
    data = json.loads(Path(prd_path).read_text(encoding="utf-8"))
    items = data.get("items", [])
    return [
        BlockedItem(id=str(item.get("id", "?")), reason=str(item.get("blockReason", "")))
        for item in items
        if item.get("blocked", False)
    ]


def build_feedback_summary(
    *,
    pr_url: str | None,
    blocked_items: Sequence[BlockedItem],
    completed_count: int = 0,
    trip: BreakerTrip | None = None,
) -> str:
    """Render the markdown feedback summary appended to the task body."""
    lines: list[str] = ["## Stromboli build summary", ""]

    lines.append(f"- **Completed:** {completed_count} item(s).")
    if pr_url:
        lines.append(f"- **Pull request:** {pr_url}")
    else:
        lines.append("- **Pull request:** none opened (no changes produced).")

    if trip is not None:
        lines.append(
            f"- **Circuit breaker:** tripped ({trip.reason.value}) after "
            f"{trip.iterations} iteration(s), ${trip.total_cost_usd:.2f}."
        )

    if blocked_items:
        lines.append("")
        lines.append("### Blocked items")
        for item in blocked_items:
            reason = item.reason or "no reason recorded"
            lines.append(f"- **{item.id}** — {reason}")

    return "\n".join(lines)


def resilient_append(
    notion: AppendGateway,
    page_id: str,
    markdown: str,
    *,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Append ``markdown`` to the task body with bounded retries.

    Returns ``True`` on success. On a permanent failure it logs and returns
    ``False`` — it never raises, so a write-back failure cannot crash the worker.
    Backs off exponentially between attempts (``sleep`` is injectable for tests).
    """
    for attempt in range(1, retries + 1):
        try:
            notion.append_task_body(page_id, markdown)
            return True
        except Exception as exc:  # noqa: BLE001 - write-back must never propagate
            logger.warning(
                "Write-back to %s failed (attempt %d/%d): %s",
                page_id,
                attempt,
                retries,
                exc,
            )
            if attempt < retries:
                sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
    logger.error("Write-back to %s gave up after %d attempts.", page_id, retries)
    return False


__all__ = [
    "AppendGateway",
    "BlockedItem",
    "build_feedback_summary",
    "read_blocked_items",
    "resilient_append",
]
