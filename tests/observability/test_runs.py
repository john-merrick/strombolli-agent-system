"""Tests for the SQLite runs registry (control plane + dashboard source)."""

from __future__ import annotations

from pathlib import Path

from stromboli.observability.runs import RunsRegistry


def _reg(tmp_path: Path) -> RunsRegistry:
    return RunsRegistry(tmp_path / "runs.db")


def test_register_and_get_run(tmp_path: Path) -> None:
    r = _reg(tmp_path)
    r.register_run("run-1", task_id="run-1", task_name="do x", source="notion", pid=123)
    run = r.get_run("run-1")
    assert run is not None
    assert run["status"] == "running" and run["pid"] == 123
    assert run["current_node"] == "intake"


def test_node_events_and_turns(tmp_path: Path) -> None:
    r = _reg(tmp_path)
    r.register_run("run-1", task_id="run-1", task_name="t", source="cli", pid=1)
    r.start_node("run-1", "spec")
    r.end_node("run-1", "spec", detail="status=specced")
    r.record_turn("run-1", 1, ["Read", "Edit"], 42)
    run = r.get_run("run-1")
    assert run is not None
    phases = [(e["node"], e["phase"]) for e in run["node_events"]]
    assert ("spec", "start") in phases and ("spec", "end") in phases
    assert run["current_node"] == "spec"
    assert run["turns"][0]["tools"] == "Read,Edit"
    assert run["turns"][0]["output_tokens"] == 42


def test_cancel_flag(tmp_path: Path) -> None:
    r = _reg(tmp_path)
    r.register_run("run-1", task_id="run-1", task_name="t", source="cli", pid=1)
    assert r.is_cancel_requested("run-1") is False
    r.request_cancel("run-1")
    assert r.is_cancel_requested("run-1") is True


def test_finish_and_summary(tmp_path: Path) -> None:
    r = _reg(tmp_path)
    r.register_run("a", task_id="a", task_name="t", source="cli", pid=1)
    r.end_node("a", "coding", detail="", output_tokens=100)
    r.finish_run("a", status="done", pr_url="https://pr/1")
    r.register_run("b", task_id="b", task_name="t2", source="cli", pid=2)
    r.finish_run("b", status="escalated")

    runs = r.list_runs()
    assert {x["run_id"] for x in runs} == {"a", "b"}
    done = next(x for x in runs if x["run_id"] == "a")
    assert done["status"] == "done" and done["pr_url"] == "https://pr/1"
    assert done["total_tokens"] == 100

    s = r.summary()
    assert s["total_runs"] == 2
    assert s["by_status"]["done"] == 1 and s["by_status"]["escalated"] == 1
    assert s["total_tokens"] == 100


def test_get_missing_run_is_none(tmp_path: Path) -> None:
    assert _reg(tmp_path).get_run("nope") is None
