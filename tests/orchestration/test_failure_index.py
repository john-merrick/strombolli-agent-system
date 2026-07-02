"""Tests for the failure-to-dataset pipeline store (self-improving §1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stromboli.orchestration.failure_index import (
    LABEL_ACCEPT,
    LABEL_REJECT,
    FailureIndex,
    FailureRecord,
)


def _rec(task_id: str = "t1", **over: object) -> FailureRecord:
    base: dict[str, Any] = dict(
        task_id=task_id, ts="2026-07-03T00:00:00+00:00", goal="add subtract",
        decision="revise", outcome="escalated", task_type="add-endpoint",
        failure_mode="missing-tests", reason="no test", fix="add a test",
        cause="forgot the test", diff="+def subtract(): ...",
        test_evidence="tests passed", acceptance_criteria="returns a-b\nhas a test",
    )
    base.update(over)
    return FailureRecord(**base)


def test_record_and_all_roundtrip(tmp_path: Path) -> None:
    idx = FailureIndex(tmp_path / "failures.db")
    idx.record(_rec())
    rows = idx.all()
    assert len(rows) == 1 and rows[0].failure_mode == "missing-tests"
    assert rows[0].fix == "add a test" and rows[0].resolved is False


def test_record_is_idempotent_and_last_write_wins(tmp_path: Path) -> None:
    idx = FailureIndex(tmp_path / "failures.db")
    idx.record(_rec(decision="revise", outcome="escalated"))
    idx.record(_rec(decision="pass", outcome="done", resolved=True))
    rows = idx.all()
    assert len(rows) == 1 and rows[0].decision == "pass" and rows[0].resolved is True


def test_unresolved_and_labelled_filters(tmp_path: Path) -> None:
    idx = FailureIndex(tmp_path / "failures.db")
    idx.record(_rec("t1", resolved=False))
    idx.record(_rec("t2", decision="pass", outcome="done", resolved=True))
    assert [r.task_id for r in idx.unresolved()] == ["t1"]
    idx.label("t2", LABEL_ACCEPT)
    assert [r.task_id for r in idx.labelled()] == ["t2"]


def test_export_verifier_dataset_shape(tmp_path: Path) -> None:
    idx = FailureIndex(tmp_path / "failures.db")
    idx.record(_rec("accepted", decision="revise"))
    idx.label("accepted", LABEL_ACCEPT)          # human agreed → gold = revise
    idx.record(_rec("rejected", decision="pass"))
    idx.label("rejected", LABEL_REJECT)          # bare reject → no gold → skipped
    out = tmp_path / "verifier_labelled.json"
    n = idx.export_verifier_dataset(out)
    assert n == 1
    payload = json.loads(out.read_text())
    assert payload["name"] == "verifier_labelled"
    assert payload["cases"][0]["expected"]["decision"] == "revise"
    assert payload["cases"][0]["inputs"]["goal"] == "add subtract"


def test_diff_is_truncated(tmp_path: Path) -> None:
    idx = FailureIndex(tmp_path / "failures.db")
    idx.record(_rec(diff="x" * 20_000))
    assert len(idx.all()[0].diff) <= FailureIndex._MAX_FIELD


def test_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "failures.db"
    FailureIndex(path).record(_rec())
    assert FailureIndex(path).all()[0].task_id == "t1"
