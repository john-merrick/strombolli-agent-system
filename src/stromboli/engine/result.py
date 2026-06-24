"""The shared build-result contract (engine Phase 0).

``pipeline.run_build`` finalizes a build by routing it, writing feedback back to
Notion, and recording a Langfuse trace. Both the legacy Ralph
:class:`~stromboli.loop.LoopResult` and the new graph engine's result must feed
that *same* finalize path, so this module extracts the tiny structural shape
they share:

* :class:`BuildResult` — a :class:`~typing.Protocol` (structural, so neither
  engine needs to import or subclass it) exposing exactly what the Integrator,
  write-back, and tracer consume; and
* :class:`UsageSpan` — one unit of agent work's cost / token usage, the value
  the observability tracer records per span.

It is deliberately dependency-light (only ``breaker`` / ``writeback`` types,
under ``TYPE_CHECKING``) so either engine can conform without an import cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from stromboli.breaker import BreakerTrip
    from stromboli.writeback import BlockedItem


@dataclass(frozen=True)
class UsageSpan:
    """One unit of agent work's cost / token usage, recorded as a trace span.

    For Ralph this is a single loop iteration; for the graph engine it is one
    node invocation. ``cost_usd`` is ``None`` when the backend reported no cost.
    """

    index: int
    cost_usd: float | None
    input_tokens: int
    output_tokens: int


@runtime_checkable
class BuildResult(Protocol):
    """The shared shape the Integrator / write-back / tracer consume.

    Both ``loop.LoopResult`` (Ralph) and the graph engine's result satisfy this
    structurally, so a single finalize path serves either engine. ``reason`` is
    each engine's terminal-reason enum (a :class:`~enum.StrEnum`, hence ``str``).
    """

    @property
    def reason(self) -> str: ...

    @property
    def tripped(self) -> bool: ...

    @property
    def trip(self) -> BreakerTrip | None: ...

    @property
    def artifact_path(self) -> Path: ...

    def blocked_items(self) -> Sequence[BlockedItem]: ...

    def completed_count(self) -> int: ...

    def usage_spans(self) -> Sequence[UsageSpan]: ...


__all__ = [
    "BuildResult",
    "UsageSpan",
]
