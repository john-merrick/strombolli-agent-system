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


def test_watch_once_dispatches_and_notifies_new_tasks() -> None:
    pushes: list[str] = []
    ran: list[str] = []

    class _Notion:
        def query_ready_tasks(self, db_id: str) -> list[Any]:
            return [make_task(page_id="pg-1"), make_task(page_id="pg-2")]

    class _Notifier:
        def notify(self, text: str) -> None:
            pushes.append(text)

    seen: set[str] = set()
    dispatched = cli._watch_once(
        _Notion(), "db", _Notifier(), seen,
        run=lambda t: ran.append(t.page_id), now=lambda: "2026-06-30T08:00:00",
    )
    assert [t.page_id for t in dispatched] == ["pg-1", "pg-2"]
    assert ran == ["pg-1", "pg-2"]
    assert any("pg-1" in p and "New task" in p for p in pushes)

    # A second pass with the same Ready tasks does NOT re-notify or re-dispatch.
    ran.clear()
    pushes.clear()
    again = cli._watch_once(
        _Notion(), "db", _Notifier(), seen,
        run=lambda t: ran.append(t.page_id), now=lambda: "later",
    )
    assert again == [] and ran == [] and pushes == []


def test_parser_accepts_watch() -> None:
    args = cli.build_parser().parse_args(["watch", "--interval", "5"])
    assert args.command == "watch" and args.interval == 5.0


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


def test_watch_once_redispatches_a_requeued_task() -> None:
    """A task that leaves the Ready queue (built/escalated) and later returns
    (human fixed it, re-ticked Ready) must be dispatched again — a permanent
    `seen` set silently ignored every re-queue until a watcher restart."""
    ran: list[str] = []

    class _Notion:
        def __init__(self) -> None:
            self.queue: list[Any] = [make_task(page_id="pg-1")]

        def query_ready_tasks(self, db_id: str) -> list[Any]:
            return list(self.queue)

    class _Notifier:
        def notify(self, text: str) -> None:
            pass

    notion = _Notion()
    seen: set[str] = set()
    kwargs = dict(run=lambda t: ran.append(t.page_id), now=lambda: "t0")

    cli._watch_once(notion, "db", _Notifier(), seen, **kwargs)
    assert ran == ["pg-1"]

    # Task built → leaves the Ready queue → forgotten.
    notion.queue = []
    cli._watch_once(notion, "db", _Notifier(), seen, **kwargs)
    assert seen == set()

    # Human re-queues it → dispatched again.
    notion.queue = [make_task(page_id="pg-1")]
    cli._watch_once(notion, "db", _Notifier(), seen, **kwargs)
    assert ran == ["pg-1", "pg-1"]
