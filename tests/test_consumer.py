"""Tests for the build consumer — the FIFO queue in front of the serial worker.

``run_once`` is tested directly (no thread) for the lifecycle mapping; one test
exercises the real thread via an Event so nothing is dropped end-to-end.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from stromboli.consumer import STAGE_BUILDING, BuildConsumer
from stromboli.ledger import RunLedger, RunState
from stromboli.worker import DispatchOutcome


def _clock() -> Callable[[], datetime]:
    state = {"t": datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)}

    def now() -> datetime:
        current = state["t"]
        state["t"] = current + timedelta(seconds=1)
        return current

    return now


def _ledger(tmp_path: Path) -> RunLedger:
    return RunLedger(tmp_path / "runs.db", clock=_clock())


class RecordingProcess:
    """A fake worker.dispatch: records calls and returns a scripted outcome."""

    def __init__(self, outcome: DispatchOutcome = DispatchOutcome.CLAIMED) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def __call__(self, page_id: str) -> DispatchOutcome:
        self.calls.append(page_id)
        return self.outcome


def test_enqueue_never_drops_even_while_busy(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    consumer = BuildConsumer(ledger, RecordingProcess())
    # Five quick dispatches all land in the queue (the old worker dropped these).
    for pid in ("a", "b", "c", "d", "e"):
        consumer.enqueue(pid)
    assert [r.page_id for r in ledger.queued()] == ["a", "b", "c", "d", "e"]


def test_run_once_builds_and_marks_done(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    process = RecordingProcess(DispatchOutcome.CLAIMED)
    consumer = BuildConsumer(ledger, process)
    run = consumer.enqueue("a")

    assert consumer.run_once() is True
    assert process.calls == ["a"]
    done = ledger.get(run.id)
    assert done.state is RunState.DONE
    assert done.outcome == "claimed"
    assert done.ended_at is not None


def test_run_once_processes_fifo(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    process = RecordingProcess()
    consumer = BuildConsumer(ledger, process)
    consumer.enqueue("a")
    consumer.enqueue("b")

    consumer.run_once()
    consumer.run_once()
    assert process.calls == ["a", "b"]
    assert consumer.run_once() is False  # drained


def test_guard_declined_dispatch_is_skipped_not_failed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    consumer = BuildConsumer(ledger, RecordingProcess(DispatchOutcome.NOT_READY))
    run = consumer.enqueue("a")

    consumer.run_once()
    skipped = ledger.get(run.id)
    assert skipped.state is RunState.SKIPPED
    assert skipped.outcome == "not_ready"
    assert skipped.error is None


def test_build_exception_marks_failed_with_error(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    def boom(page_id: str) -> DispatchOutcome:
        raise RuntimeError("kaboom")

    consumer = BuildConsumer(ledger, boom)
    run = consumer.enqueue("a")

    consumer.run_once()  # must not raise — one build can't kill the consumer
    failed = ledger.get(run.id)
    assert failed.state is RunState.FAILED
    assert "kaboom" in (failed.error or "")


def test_running_run_is_stamped_with_a_stage(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    seen_stage: list[str | None] = []

    def process(page_id: str) -> DispatchOutcome:
        # While building, the run is RUNNING with a stage set.
        running = ledger.running()
        seen_stage.append(running.stage if running else None)
        return DispatchOutcome.CLAIMED

    consumer = BuildConsumer(ledger, process)
    consumer.enqueue("a")
    consumer.run_once()
    assert seen_stage == [STAGE_BUILDING]


def test_run_once_on_empty_queue_returns_false(tmp_path: Path) -> None:
    consumer = BuildConsumer(_ledger(tmp_path), RecordingProcess())
    assert consumer.run_once() is False


def test_consumer_thread_drains_the_queue(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    built = threading.Event()

    def process(page_id: str) -> DispatchOutcome:
        built.set()
        return DispatchOutcome.CLAIMED

    consumer = BuildConsumer(ledger, process, poll_interval=0.01)
    run = consumer.enqueue("a")
    consumer.start()
    try:
        assert built.wait(timeout=3.0), "the consumer thread never built the run"
    finally:
        consumer.stop()
    # Give the finish() write a moment; stop() joined the thread so it's done.
    assert ledger.get(run.id).state is RunState.DONE
