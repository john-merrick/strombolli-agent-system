"""Tests for the terminal status command."""

from __future__ import annotations

from pathlib import Path

import pytest

from stromboli.cli import ledger_path, main, render
from stromboli.ledger import RunLedger, RunState


def test_ledger_path_prefers_explicit_override() -> None:
    env = {"STROMBOLI_LEDGER_PATH": "/tmp/x/runs.db", "WORKSPACE_ROOT": "/ws"}
    assert ledger_path(env) == Path("/tmp/x/runs.db")


def test_ledger_path_derives_from_workspace() -> None:
    path = ledger_path({"WORKSPACE_ROOT": "/ws"})
    assert path == Path("/ws") / ".stromboli" / "runs.db"


def test_ledger_path_errors_without_config() -> None:
    with pytest.raises(SystemExit):
        ledger_path({})


def test_render_shows_running_queue_recent_and_metrics() -> None:
    status = {
        "running": {"page_id": "p2", "task_name": "Build API", "stage": "RunVerifier: met"},
        "queued": [{"page_id": "p3", "task_name": "Docs", "state": "queued"}],
        "recent": [{"page_id": "p1", "task_name": "Health", "state": "done"}],
    }
    metrics = {
        "sample_size": 1,
        "outcomes": {"done": 1},
        "build_seconds": {"count": 1, "avg_seconds": 12.0, "max_seconds": 12.0},
        "queue_wait_seconds": {"count": 1, "avg_seconds": 1.0, "max_seconds": 1.0},
    }
    report = render(status, metrics)
    assert "Build API" in report and "RunVerifier: met" in report
    assert "QUEUED (1)" in report and "Docs" in report
    assert "RECENT" in report and "Health" in report
    assert "avg 12.0s" in report
    assert "{'done': 1}" in report


def test_render_handles_idle_empty_queue() -> None:
    report = render(
        {"running": None, "queued": [], "recent": []},
        {"sample_size": 0, "build_seconds": {}, "queue_wait_seconds": {}},
    )
    assert "RUNNING: (idle)" in report
    assert "QUEUED: (none)" in report
    assert "build time: n/a" in report


def test_main_prints_a_report_for_a_real_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "runs.db"
    ledger = RunLedger(db)
    run = ledger.enqueue("p1", task_name="Add healthcheck")
    ledger.claim_next()
    ledger.finish(run.id, RunState.DONE)

    monkeypatch.setenv("STROMBOLI_LEDGER_PATH", str(db))
    main()

    out = capsys.readouterr().out
    assert "Stromboli — build queue" in out
    assert "Add healthcheck" in out
