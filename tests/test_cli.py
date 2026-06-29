"""Tests for the CLI surface (run / poll)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from stromboli import __main__ as cli
from stromboli.state import StromboliState
from tests.nodes._fakes import make_task


def test_parser_accepts_run_and_poll() -> None:
    parser = cli.build_parser()
    run = parser.parse_args(["run", "--task", "do x"])
    assert run.command == "run" and run.task == "do x"
    poll = parser.parse_args(["poll"])
    assert poll.command == "poll"


def test_poll_runs_every_ready_task(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    class FakeNotionClient:
        def __init__(self, token: str) -> None:
            pass

        def query_ready_tasks(self, db_id: str) -> list[Any]:
            return [make_task(page_id="pg-1"), make_task(page_id="pg-2")]

    def fake_run_task(raw: str, *, source: str, task_id: str, settings: Any) -> StromboliState:
        ran.append(task_id)
        return StromboliState(task_id=task_id, source="notion", raw_request="x",
                              status="done")

    monkeypatch.setattr("stromboli.settings.load_settings",
        lambda: SimpleNamespace(notion_token="t", notion_task_db_id="db"))
    monkeypatch.setattr(
        "stromboli.integrations.notion.NotionTaskClient", FakeNotionClient
    )
    monkeypatch.setattr("stromboli.graph.run_task", fake_run_task)

    assert cli._poll() == 0
    assert ran == ["pg-1", "pg-2"]


def test_poll_with_no_ready_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeNotionClient:
        def __init__(self, token: str) -> None:
            pass

        def query_ready_tasks(self, db_id: str) -> list[Any]:
            return []

    monkeypatch.setattr("stromboli.settings.load_settings",
        lambda: SimpleNamespace(notion_token="t", notion_task_db_id="db"))
    monkeypatch.setattr(
        "stromboli.integrations.notion.NotionTaskClient", FakeNotionClient
    )
    assert cli._poll() == 0
