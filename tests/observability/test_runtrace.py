"""Tests for the local per-run trace recorder."""

from __future__ import annotations

import json
from pathlib import Path

from stromboli.observability.runtrace import FileRunTrace, RunTrace
from stromboli.state import TestResult, Verdict


def test_null_runtrace_is_noop() -> None:
    RunTrace().record("spec", {"status": "specced"})
    RunTrace().event("error", "boom")  # no exception


def test_file_runtrace_writes_per_node_records(tmp_path: Path) -> None:
    trace = FileRunTrace(tmp_path, "task-1")
    trace.record("spec", {"status": "specced"})
    trace.record(
        "coding",
        {
            "code_diff": "x" * 9000,
            "test_results": [TestResult(passed=False, summary="1 failed")],
            "status": "coding",
        },
    )
    trace.record("verifier", {"verdict": Verdict(decision="revise", reason="weak")})
    trace.event("escalation", "rate-limited; resume later")

    jsonl = (tmp_path / ".stromboli" / "runs" / "task-1" / "trace.jsonl").read_text()
    lines = [json.loads(line) for line in jsonl.strip().splitlines()]
    assert [x.get("node") or x.get("event") for x in lines] == [
        "spec", "coding", "verifier", "escalation",
    ]
    # Big diff is summarized, not dumped whole.
    coding = lines[1]["output"]
    assert coding["code_diff_chars"] == 9000
    assert len(coding["code_diff_head"]) <= 400
    assert coding["test_results"][0]["passed"] is False
    assert lines[2]["output"]["verdict"]["decision"] == "revise"
    # A human-readable companion is written too.
    assert (tmp_path / ".stromboli" / "runs" / "task-1" / "trace.md").exists()
