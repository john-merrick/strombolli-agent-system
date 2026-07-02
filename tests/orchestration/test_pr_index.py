"""Tests for the PR watch index (feedback loop persistence)."""

from __future__ import annotations

from pathlib import Path

from stromboli.orchestration.pr_index import CLOSED, WATCHING, PRIndex, PRWatch


def _watch(**over: object) -> PRWatch:
    base = dict(
        task_id="t1", repo="o/r", branch="stromboli/t1-x", pr_number=5,
        pr_url="https://gh/o/r/pull/5", goal="do x", session_id="sess-1",
    )
    base.update(over)
    return PRWatch(**base)  # type: ignore[arg-type]


def test_register_and_active_roundtrip(tmp_path: Path) -> None:
    idx = PRIndex(tmp_path / "prs.db")
    idx.register(_watch(), now="2026-07-02T00:00:00+00:00")
    active = idx.active()
    assert len(active) == 1 and active[0].pr_number == 5
    assert active[0].session_id == "sess-1" and active[0].state == WATCHING


def test_register_is_idempotent_on_task_id(tmp_path: Path) -> None:
    idx = PRIndex(tmp_path / "prs.db")
    idx.register(_watch(), now="t0")
    idx.register(_watch(pr_url="https://gh/o/r/pull/5#2"), now="t1")
    assert len(idx.active()) == 1


def test_update_and_closed_drops_from_active(tmp_path: Path) -> None:
    idx = PRIndex(tmp_path / "prs.db")
    idx.register(_watch(), now="t0")
    idx.update("t1", now="t1", fix_rounds=1, last_ci_sha="abc")
    assert idx.active()[0].fix_rounds == 1
    assert idx.active()[0].last_ci_sha == "abc"
    idx.update("t1", now="t2", state=CLOSED)
    assert idx.active() == []


def test_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "prs.db"
    PRIndex(path).register(_watch(), now="t0")
    assert PRIndex(path).active()[0].task_id == "t1"  # a fresh process sees it
