"""Tests for the resilient outcome write-back (US-012).

On completion — success, blocked items, or a breaker trip — the worker appends a
feedback summary to the task body (what was done, what's blocked and why, the PR
link). The write-back is resilient: a transient Notion failure is retried and
logged, and a permanent failure does not crash the worker.

The summary builder is pure (asserted directly), and resilience is exercised
with a fake Notion that fails a configurable number of times before succeeding.
"""

from __future__ import annotations

import json
from pathlib import Path

from stromboli.breaker import BreakerTrip, TripReason
from stromboli.writeback import (
    BlockedItem,
    build_feedback_summary,
    read_blocked_items,
    resilient_append,
)

# --------------------------------------------------------------------------- #
# read_blocked_items                                                          #
# --------------------------------------------------------------------------- #


def test_read_blocked_items_extracts_id_and_reason(tmp_path: Path) -> None:
    prd = tmp_path / "prd.json"
    prd.write_text(
        json.dumps(
            {
                "items": [
                    {"id": "AC-001", "passes": True},
                    {
                        "id": "AC-002",
                        "blocked": True,
                        "blockReason": "migration needs manual reconciliation",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    blocked = read_blocked_items(prd)

    assert blocked == [
        BlockedItem(id="AC-002", reason="migration needs manual reconciliation")
    ]


# --------------------------------------------------------------------------- #
# build_feedback_summary                                                      #
# --------------------------------------------------------------------------- #


def test_summary_includes_pr_link_and_completed_count() -> None:
    summary = build_feedback_summary(
        pr_url="https://github.com/o/r/pull/3",
        blocked_items=[],
        completed_count=4,
    )
    assert "https://github.com/o/r/pull/3" in summary
    assert "4" in summary


def test_summary_includes_blocked_item_reasons_when_present() -> None:
    summary = build_feedback_summary(
        pr_url=None,
        blocked_items=[
            BlockedItem(id="AC-002", reason="migration needs manual reconciliation"),
            BlockedItem(id="AC-005", reason="external API credentials missing"),
        ],
        completed_count=2,
    )
    # Every blocked item id AND its reason must surface for triage.
    assert "AC-002" in summary
    assert "migration needs manual reconciliation" in summary
    assert "AC-005" in summary
    assert "external API credentials missing" in summary


def test_summary_notes_a_breaker_trip() -> None:
    trip = BreakerTrip(
        reason=TripReason.COST, iterations=6, total_cost_usd=3.0, limit=2.0
    )
    summary = build_feedback_summary(
        pr_url=None, blocked_items=[], completed_count=1, trip=trip
    )
    assert "circuit breaker" in summary.lower()


# --------------------------------------------------------------------------- #
# resilient_append                                                            #
# --------------------------------------------------------------------------- #


class FlakyNotion:
    """Raises on the first ``fail_times`` appends, then succeeds."""

    def __init__(self, *, fail_times: int) -> None:
        self._fail_times = fail_times
        self.attempts = 0
        self.appended: list[str] = []

    def append_task_body(self, page_id: str, markdown: str) -> None:
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise RuntimeError("notion 503")
        self.appended.append(markdown)


def test_resilient_append_retries_then_succeeds() -> None:
    notion = FlakyNotion(fail_times=2)
    sleeps: list[float] = []

    ok = resilient_append(
        notion, "page-1", "summary", retries=5, sleep=sleeps.append
    )

    assert ok is True
    assert notion.attempts == 3  # failed twice, succeeded on the third
    assert notion.appended == ["summary"]
    assert len(sleeps) == 2  # backed off between the two retries


def test_resilient_append_gives_up_without_crashing() -> None:
    notion = FlakyNotion(fail_times=99)  # never succeeds

    # Must NOT raise — a permanent Notion failure cannot crash the worker.
    ok = resilient_append(notion, "page-1", "summary", retries=3, sleep=lambda _: None)

    assert ok is False
    assert notion.attempts == 3  # tried exactly `retries` times
    assert notion.appended == []
