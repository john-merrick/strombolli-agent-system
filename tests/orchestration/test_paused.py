"""Tests for the paused-task index (the suspend/resume foundation)."""

from __future__ import annotations

from pathlib import Path

from stromboli.orchestration.paused import PausedIndex
from stromboli.state import Spec, StromboliState


def _state(task_id: str = "t1") -> StromboliState:
    return StromboliState(task_id=task_id, source="notion", raw_request="do x")


def test_suspend_assigns_sequential_refs(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    a = idx.suspend(_state("a"), reason="r1")
    b = idx.suspend(_state("b"), reason="r2")
    assert (a.ref, b.ref) == (1, 2)


def test_resolve_frees_ref_for_reuse(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("a"), reason="r")  # #1
    idx.suspend(_state("b"), reason="r")  # #2
    idx.resolve("a", state="resumed")
    c = idx.suspend(_state("c"), reason="r")  # smallest free is now #1
    assert c.ref == 1
    by_ref = idx.by_ref(1)
    assert by_ref is not None and by_ref.task_id == "c"


def test_get_by_ref_and_open_tasks(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("a"), reason="r")
    got = idx.get("a")
    assert got is not None and got.task_id == "a"
    assert [t.task_id for t in idx.open_tasks()] == ["a"]
    idx.resolve("a", state="resumed")
    assert idx.by_ref(1) is None
    assert idx.open_tasks() == []


def test_resuspend_keeps_ref_and_updates_state(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("a").model_copy(update={"session_id": "s1"}), reason="r1")
    again = idx.suspend(_state("a").model_copy(update={"session_id": "s2"}), reason="r2")
    assert again.ref == 1
    restored = idx.load_state("a")
    assert restored is not None and restored.session_id == "s2"
    row = idx.get("a")
    assert row is not None and row.reason == "r2"


def test_load_state_round_trips(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    s = _state("a").model_copy(
        update={"session_id": "sess", "spec": Spec(goal="g"), "outer_iterations": 2}
    )
    idx.suspend(s, reason="r")
    got = idx.load_state("a")
    assert got is not None
    assert got.session_id == "sess"
    assert got.spec is not None and got.spec.goal == "g"
    assert got.outer_iterations == 2


def test_expired_returns_old_open_rows_only(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("old"), reason="r", paused_at="2020-01-01T00:00:00")
    idx.suspend(_state("new"), reason="r", paused_at="2999-01-01T00:00:00")
    expired = idx.expired("2025-01-01T00:00:00")
    assert [t.task_id for t in expired] == ["old"]
