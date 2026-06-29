"""Tests for the watchtower dashboard API + cancel control."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from stromboli.dashboard.app import create_dashboard
from stromboli.observability.runs import RunsRegistry


def _client(tmp_path: Path) -> tuple[TestClient, RunsRegistry]:
    reg = RunsRegistry(tmp_path / "runs.db")
    return TestClient(create_dashboard(reg)), reg


def test_index_serves_html(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Stromboli Watchtower" in resp.text


def test_runs_and_summary_endpoints(tmp_path: Path) -> None:
    client, reg = _client(tmp_path)
    reg.register_run("r1", task_id="r1", task_name="do x", source="notion", pid=1)
    reg.finish_run("r1", status="done", pr_url="https://pr/1")

    runs = client.get("/api/runs").json()
    assert runs[0]["run_id"] == "r1" and runs[0]["status"] == "done"

    detail = client.get("/api/runs/r1").json()
    assert detail["pr_url"] == "https://pr/1"

    summary = client.get("/api/summary").json()
    assert summary["total_runs"] == 1 and summary["by_status"]["done"] == 1

    assert client.get("/api/runs/missing").status_code == 404


def test_cancel_sets_flag(tmp_path: Path) -> None:
    client, reg = _client(tmp_path)
    reg.register_run("r1", task_id="r1", task_name="t", source="cli", pid=999999)
    resp = client.post("/api/runs/r1/cancel")
    assert resp.status_code == 200 and resp.json()["cancel_requested"] is True
    assert reg.is_cancel_requested("r1") is True


def test_cancel_missing_run_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.post("/api/runs/nope/cancel").status_code == 404
