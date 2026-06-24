"""Tests for per-step transcripts (the build's on-disk black box)."""

from __future__ import annotations

from pathlib import Path

from stromboli.engine.state import STATE_DIR, TRANSCRIPTS_DIR
from stromboli.engine.transcript import (
    Exchange,
    StepRecord,
    TranscriptRecorder,
    render_transcript,
    write_transcript,
)


def test_filename_is_zero_padded_and_ordered() -> None:
    assert StepRecord(step=2, action="RunWorker").filename == "002-RunWorker.md"
    assert StepRecord(step=12, action="RunVerifier").filename == "012-RunVerifier.md"


def test_render_includes_action_outcome_and_exchange() -> None:
    record = StepRecord(
        step=3,
        action="RunVerifier",
        outcome="verdict: not met (hollow tests)",
        exchanges=(Exchange(prompt="judge this diff", raw_output='{"met": false}'),),
    )
    text = render_transcript(record)
    assert "Step 003 — RunVerifier" in text
    assert "verdict: not met (hollow tests)" in text
    assert "judge this diff" in text
    assert '{"met": false}' in text


def test_render_includes_gate_section_for_worker() -> None:
    record = StepRecord(
        step=2,
        action="RunWorker",
        outcome="gate failed → retry",
        gate="uv run pytest -q (failed)\n1 failed",
        exchanges=(Exchange(prompt="implement it", raw_output="done"),),
    )
    text = render_transcript(record)
    assert "## Objective gate" in text
    assert "1 failed" in text


def test_render_numbers_multiple_exchanges() -> None:
    record = StepRecord(
        step=1,
        action="RunPlanner",
        exchanges=(
            Exchange(prompt="p1", raw_output="garbage"),
            Exchange(prompt="p2 with schema", raw_output='{"units": []}'),
        ),
    )
    text = render_transcript(record)
    assert "Exchange 1 — prompt" in text
    assert "Exchange 2 — prompt" in text
    assert "p2 with schema" in text


def test_render_notes_when_no_exchange() -> None:
    text = render_transcript(StepRecord(step=9, action="Integrate", outcome="success"))
    assert "no agent call" in text


def test_write_creates_file_in_transcripts_dir(tmp_path: Path) -> None:
    record = StepRecord(
        step=1,
        action="RunPlanner",
        outcome="2 units",
        exchanges=(Exchange(prompt="plan it", raw_output='{"units":[]}'),),
    )
    path = write_transcript(tmp_path, record)
    assert path == tmp_path / STATE_DIR / TRANSCRIPTS_DIR / "001-RunPlanner.md"
    assert path.exists()
    assert "plan it" in path.read_text(encoding="utf-8")


def test_recorder_accumulates_and_drains() -> None:
    recorder = TranscriptRecorder()
    recorder.add("p1", "o1")
    recorder.add("p2", "o2")
    first = recorder.drain()
    assert first == (Exchange("p1", "o1"), Exchange("p2", "o2"))
    # Drained: a second drain is empty until more are added.
    assert recorder.drain() == ()
    recorder.add("p3", "o3")
    assert recorder.drain() == (Exchange("p3", "o3"),)
