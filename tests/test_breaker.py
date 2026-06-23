"""Tests for the per-task circuit breaker (US-008).

The breaker bounds a single task build by two ceilings — cumulative cost (USD)
and iteration count — so a runaway loop can neither burn unlimited spend nor
spin forever. When a ceiling trips the loop stops, the task is routed to
``Review``, and a note explaining the trip is appended to the task body.

The breaker mechanism is a pure accumulator (no I/O), so the trip decision is
asserted directly; :func:`handle_trip` is the only part that touches Notion and
is tested against a recording fake.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stromboli.breaker import (
    BreakerConfig,
    BreakerTrip,
    CircuitBreaker,
    TripReason,
    handle_trip,
)
from stromboli.notion import STATUS_REVIEW


def _config(*, max_iterations: int = 25, max_cost_usd: float = 5.0) -> BreakerConfig:
    return BreakerConfig(max_iterations=max_iterations, max_cost_usd=max_cost_usd)


# --------------------------------------------------------------------------- #
# Config validation                                                           #
# --------------------------------------------------------------------------- #


def test_config_rejects_non_positive_ceilings() -> None:
    with pytest.raises(ValueError):
        BreakerConfig(max_iterations=0, max_cost_usd=1.0)
    with pytest.raises(ValueError):
        BreakerConfig(max_iterations=1, max_cost_usd=0.0)


# --------------------------------------------------------------------------- #
# Cost ceiling                                                                #
# --------------------------------------------------------------------------- #


def test_cost_over_ceiling_trips_the_breaker() -> None:
    breaker = CircuitBreaker(_config(max_cost_usd=1.0))

    assert breaker.record_iteration(0.40) is None  # cumulative 0.40
    trip = breaker.record_iteration(0.80)  # cumulative 1.20 > 1.0

    assert trip is not None
    assert trip.reason is TripReason.COST
    assert trip.total_cost_usd == pytest.approx(1.20)
    assert trip.iterations == 2


def test_cost_exactly_at_ceiling_does_not_trip() -> None:
    breaker = CircuitBreaker(_config(max_cost_usd=1.0))
    # At the ceiling (not over) is allowed; only strictly exceeding trips.
    assert breaker.record_iteration(1.0) is None


def test_missing_cost_is_treated_as_zero() -> None:
    breaker = CircuitBreaker(_config(max_cost_usd=1.0, max_iterations=10))
    assert breaker.record_iteration(None) is None
    assert breaker.total_cost_usd == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Iteration ceiling                                                           #
# --------------------------------------------------------------------------- #


def test_iteration_ceiling_trips_the_breaker() -> None:
    breaker = CircuitBreaker(_config(max_iterations=3, max_cost_usd=100.0))

    assert breaker.record_iteration(0.0) is None  # 1
    assert breaker.record_iteration(0.0) is None  # 2
    trip = breaker.record_iteration(0.0)  # 3 -> reached ceiling

    assert trip is not None
    assert trip.reason is TripReason.ITERATIONS
    assert trip.iterations == 3


def test_cost_ceiling_takes_precedence_over_iterations() -> None:
    # When both would trip on the same iteration, cost is reported.
    breaker = CircuitBreaker(_config(max_iterations=1, max_cost_usd=0.5))
    trip = breaker.record_iteration(0.6)
    assert trip is not None
    assert trip.reason is TripReason.COST


# --------------------------------------------------------------------------- #
# Trip note                                                                   #
# --------------------------------------------------------------------------- #


def test_trip_note_names_the_reason_and_totals() -> None:
    trip = BreakerTrip(
        reason=TripReason.COST,
        iterations=4,
        total_cost_usd=2.5,
        limit=2.0,
    )
    note = trip.note
    assert "cost" in note.lower()
    assert "2.5" in note
    assert "2.0" in note
    assert "4" in note


# --------------------------------------------------------------------------- #
# Routing the trip to Review                                                  #
# --------------------------------------------------------------------------- #


class FakeNotion:
    """Records status writes and appended body text."""

    def __init__(self) -> None:
        self.status: str | None = None
        self.appended: list[str] = []

    def update_task(self, page_id: str, *, status: str | None = None, **_: Any) -> None:
        if status is not None:
            self.status = status

    def append_task_body(self, page_id: str, markdown: str) -> None:
        self.appended.append(markdown)


def test_handle_trip_routes_to_review_and_appends_the_note() -> None:
    notion = FakeNotion()
    trip = BreakerTrip(
        reason=TripReason.COST,
        iterations=4,
        total_cost_usd=2.5,
        limit=2.0,
    )

    handle_trip(notion, "page-1", trip)

    assert notion.status == STATUS_REVIEW
    assert len(notion.appended) == 1
    assert "cost" in notion.appended[0].lower()


# --------------------------------------------------------------------------- #
# End-to-end: the loop trips the breaker and the task is routed to Review      #
# --------------------------------------------------------------------------- #


def test_loop_stops_on_breaker_and_trip_routes_to_review(tmp_path: Path) -> None:
    import json
    from collections.abc import Sequence

    from stromboli.loop import RalphLoop, StopReason
    from stromboli.prd import PRD_RELATIVE_PATH

    # A PRD that stays eligible forever and a CC stub that never completes but
    # reports cost each turn: the only thing that can stop it is the breaker.
    prd_path = tmp_path / PRD_RELATIVE_PATH
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        json.dumps({"meta": {}, "items": [{"id": "a"}]}), encoding="utf-8"
    )

    def cc(argv: Sequence[str], cwd: Path) -> str:
        return json.dumps({"result": "still grinding", "total_cost_usd": 0.8})

    breaker = CircuitBreaker(_config(max_cost_usd=1.0, max_iterations=100))
    # Loop ceiling is generous; the cost breaker must trip first (0.8 + 0.8 > 1).
    result = RalphLoop(run=cc, max_iterations=50, breaker=breaker).run(tmp_path)

    assert result.reason is StopReason.BREAKER
    assert result.tripped is True
    assert result.trip is not None
    assert result.trip.reason is TripReason.COST
    assert len(result.iterations) == 2  # tripped on the second iteration

    # The trip then routes the task to Review with an explanatory note.
    notion = FakeNotion()
    handle_trip(notion, "page-1", result.trip)
    assert notion.status == STATUS_REVIEW
    assert notion.appended and "cost" in notion.appended[0].lower()
