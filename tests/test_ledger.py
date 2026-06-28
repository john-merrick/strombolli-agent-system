"""Tests for the run ledger — the durable dispatch lifecycle record.

A fake monotonic clock makes the lifecycle timestamps deterministic; every test
uses a real temp-file SQLite db so persistence across reopen is exercised.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stromboli.ledger import (
    RunLedger,
    RunState,
    compute_metrics,
    metrics_snapshot,
)


def _clock() -> Callable[[], datetime]:
    """A monotonic fake clock advancing one second per read."""
    state = {"t": datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)}

    def now() -> datetime:
        current = state["t"]
        state["t"] = current + timedelta(seconds=1)
        return current

    return now


def _ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "runs.db", clock=_clock())


def test_enqueue_creates_a_queued_run(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    run = ledger.enqueue("page-1", task_name="Add healthcheck", engine="graph")
    assert run.id > 0
    assert run.page_id == "page-1"
    assert run.task_name == "Add healthcheck"
    assert run.engine == "graph"
    assert run.state is RunState.QUEUED
    assert run.queued_at
    assert run.started_at is None
    assert not run.is_terminal


def test_claim_next_is_fifo_and_marks_running(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.enqueue("a")
    second = ledger.enqueue("b")

    claimed = ledger.claim_next()
    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.state is RunState.RUNNING
    assert claimed.started_at is not None
    # The second is still waiting.
    assert [r.page_id for r in ledger.queued()] == ["b"]
    running = ledger.running()
    assert running is not None and running.page_id == "a"

    second_claim = ledger.claim_next()
    assert second_claim is not None and second_claim.id == second.id
    assert ledger.claim_next() is None  # queue drained


def test_position_counts_runs_ahead(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    a = ledger.enqueue("a")
    b = ledger.enqueue("b")
    c = ledger.enqueue("c")
    assert ledger.position(a.id) == 0
    assert ledger.position(b.id) == 1
    assert ledger.position(c.id) == 2
    # Claiming the head shifts everyone forward.
    ledger.claim_next()
    assert ledger.position(b.id) == 0
    assert ledger.position(c.id) == 1


def test_set_stage_updates_stage_and_heartbeat(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    run = ledger.enqueue("a")
    ledger.claim_next()
    ledger.set_stage(run.id, "verifier (AC-002)")
    reloaded = ledger.get(run.id)
    assert reloaded.stage == "verifier (AC-002)"
    assert reloaded.heartbeat_at is not None


def test_finish_moves_to_terminal_and_stamps_ended(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    run = ledger.enqueue("a")
    ledger.claim_next()
    ledger.finish(run.id, RunState.DONE, outcome="integrated")
    done = ledger.get(run.id)
    assert done.state is RunState.DONE
    assert done.outcome == "integrated"
    assert done.ended_at is not None
    assert done.is_terminal


def test_finish_rejects_non_terminal_state(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    run = ledger.enqueue("a")
    with pytest.raises(ValueError, match="not a terminal state"):
        ledger.finish(run.id, RunState.RUNNING)


def test_failed_run_records_the_error(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    run = ledger.enqueue("a")
    ledger.claim_next()
    ledger.finish(run.id, RunState.FAILED, error="RuntimeError: boom")
    failed = ledger.get(run.id)
    assert failed.state is RunState.FAILED
    assert failed.error == "RuntimeError: boom"


def test_recent_returns_finished_newest_first(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for pid in ("a", "b", "c"):
        run = ledger.enqueue(pid)
        ledger.claim_next()
        ledger.finish(run.id, RunState.DONE)
    # 'd' is still queued → not in recent.
    ledger.enqueue("d")
    recent = ledger.recent()
    assert [r.page_id for r in recent] == ["c", "b", "a"]


def test_get_missing_run_raises(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(KeyError):
        ledger.get(999)


def test_compute_metrics_rolls_up_timing_and_outcomes(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)  # fake clock advances 1s per timestamp read
    # Run A: queued@t0, started@t1 (wait 1s), ended@t2 (build 1s), DONE.
    a = ledger.enqueue("a")
    ledger.claim_next()
    ledger.finish(a.id, RunState.DONE)
    # Run B: same 1s wait / 1s build, FAILED.
    b = ledger.enqueue("b")
    ledger.claim_next()
    ledger.finish(b.id, RunState.FAILED, error="boom")

    metrics = compute_metrics(ledger.recent(100))
    assert metrics["sample_size"] == 2
    assert metrics["outcomes"] == {"done": 1, "failed": 1}
    assert metrics["build_seconds"]["count"] == 2
    assert metrics["build_seconds"]["avg_seconds"] == 1.0
    assert metrics["queue_wait_seconds"]["count"] == 2


def test_compute_metrics_handles_no_runs() -> None:
    metrics = compute_metrics([])
    assert metrics["sample_size"] == 0
    assert metrics["build_seconds"] == {
        "count": 0,
        "avg_seconds": None,
        "max_seconds": None,
    }


def test_metrics_snapshot_includes_queue_depth_and_running(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    done = ledger.enqueue("a")
    ledger.claim_next()
    ledger.finish(done.id, RunState.DONE)
    ledger.enqueue("b")  # running next
    ledger.claim_next()
    ledger.enqueue("c")  # still queued

    snapshot = metrics_snapshot(ledger)
    assert snapshot["sample_size"] == 1
    assert snapshot["queue_depth"] == 1
    assert snapshot["running"] == "b"


def test_ledger_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    first = RunLedger(path, clock=_clock())
    run = first.enqueue("a", task_name="persist me")

    # A fresh handle on the same file sees the queued run.
    reopened = RunLedger(path, clock=_clock())
    again = reopened.get(run.id)
    assert again.page_id == "a"
    assert again.task_name == "persist me"
    assert again.state is RunState.QUEUED
    assert [r.page_id for r in reopened.queued()] == ["a"]
