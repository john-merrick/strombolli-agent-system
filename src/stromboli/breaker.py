"""Per-task circuit breaker (US-008).

A task build must not be able to burn unlimited cost or loop forever. The
:class:`CircuitBreaker` bounds a single build by two configurable ceilings:

* **cost** — the cumulative USD reported across Ralph iterations; and
* **iterations** — how many iterations the loop may run.

The breaker is a pure accumulator: :meth:`CircuitBreaker.record_iteration` is
called once per completed iteration with that iteration's cost and returns a
:class:`BreakerTrip` the moment a ceiling is exceeded (else ``None``). The loop
consults it and stops on a trip (see :mod:`stromboli.loop`).

:func:`handle_trip` is the side-effecting half: on a trip it routes the task to
``Review`` and appends a human-readable note explaining why, so a supervisor can
triage from Notion. It is kept separate from the accumulator so the decision
logic stays I/O-free and unit-testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from stromboli.notion import STATUS_REVIEW

logger = logging.getLogger(__name__)


class TripReason(StrEnum):
    """Which ceiling tripped the breaker."""

    #: Cumulative cost exceeded ``max_cost_usd``.
    COST = "cost_ceiling"
    #: Iteration count reached ``max_iterations``.
    ITERATIONS = "iteration_ceiling"


@dataclass(frozen=True)
class BreakerConfig:
    """Per-task ceilings. Both must be positive."""

    max_iterations: int
    max_cost_usd: float

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be > 0")


@dataclass(frozen=True)
class BreakerTrip:
    """A tripped ceiling: why it tripped and the totals at the trip."""

    reason: TripReason
    iterations: int
    total_cost_usd: float
    #: The ceiling value that was crossed (cost USD or iteration count).
    limit: float

    @property
    def note(self) -> str:
        """A markdown note explaining the trip, appended to the task body."""
        if self.reason is TripReason.COST:
            detail = (
                f"cost ceiling of ${self.limit:.2f} exceeded "
                f"(spent ${self.total_cost_usd:.2f})"
            )
        else:
            detail = f"iteration ceiling of {int(self.limit)} reached"
        return (
            f"⚠️ **Circuit breaker tripped** — {detail} after "
            f"{self.iterations} iteration(s). The build was stopped and routed "
            f"to Review for a human to triage."
        )


class TaskGateway(Protocol):
    """The slice of the Notion client :func:`handle_trip` needs."""

    def update_task(self, page_id: str, *, status: str | None = ...) -> None: ...

    def append_task_body(self, page_id: str, markdown: str) -> None: ...


class CircuitBreaker:
    """Accumulates per-iteration cost / count and trips on a ceiling."""

    def __init__(self, config: BreakerConfig) -> None:
        self._config = config
        self._iterations = 0
        self._cost = 0.0

    @property
    def iterations(self) -> int:
        """Iterations recorded so far."""
        return self._iterations

    @property
    def total_cost_usd(self) -> float:
        """Cumulative USD recorded so far."""
        return self._cost

    def record_iteration(self, cost_usd: float | None) -> BreakerTrip | None:
        """Record one completed iteration; return a trip if a ceiling crossed.

        ``cost_usd`` of ``None`` (CC reported no cost) counts as zero. Cost is
        checked before the iteration ceiling so a same-iteration double-cross is
        reported as a cost trip (the more actionable signal).
        """
        self._iterations += 1
        self._cost += cost_usd or 0.0

        if self._cost > self._config.max_cost_usd:
            return self._trip(TripReason.COST, self._config.max_cost_usd)
        if self._iterations >= self._config.max_iterations:
            return self._trip(TripReason.ITERATIONS, self._config.max_iterations)
        return None

    def _trip(self, reason: TripReason, limit: float) -> BreakerTrip:
        return BreakerTrip(
            reason=reason,
            iterations=self._iterations,
            total_cost_usd=self._cost,
            limit=limit,
        )


def handle_trip(notion: TaskGateway, page_id: str, trip: BreakerTrip) -> None:
    """Route a tripped task to ``Review`` and append the explanatory note.

    The status write happens first so the task is parked even if the body
    append later fails; the caller (write-back, US-012) makes the append
    resilient.
    """
    logger.warning(
        "Circuit breaker tripped for %s: %s after %d iteration(s), $%.2f.",
        page_id,
        trip.reason.value,
        trip.iterations,
        trip.total_cost_usd,
    )
    notion.update_task(page_id, status=STATUS_REVIEW)
    notion.append_task_body(page_id, trip.note)


__all__ = [
    "BreakerConfig",
    "BreakerTrip",
    "CircuitBreaker",
    "TaskGateway",
    "TripReason",
    "handle_trip",
]
