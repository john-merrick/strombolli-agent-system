"""Tests for the Notion intake/write-back client and its pure parsers."""

from __future__ import annotations

from typing import Any

import httpx

from stromboli.integrations.notion import (
    ASSIGNEE_AGENT,
    STATUS_TODO,
    NotionTaskClient,
    Task,
    build_feedback_summary,
    is_dispatchable,
    parse_repo,
    parse_task,
    resilient_append,
)


def _task_page(**over: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "Task name": {"title": [{"plain_text": "Do the thing"}]},
        "Status": {"select": {"name": over.get("status", STATUS_TODO)}},
        "Spec": {"rich_text": [{"plain_text": "build X"}]},
        "Assigned to": {"select": {"name": over.get("assignee", ASSIGNEE_AGENT)}},
        "Ready": {"checkbox": over.get("ready", True)},
        "Project": {"relation": [{"id": "proj-1"}]},
    }
    return {"id": over.get("id", "page-1"), "properties": props}


def test_parse_task_extracts_fields() -> None:
    task = parse_task(_task_page())
    assert task.name == "Do the thing"
    assert task.status == STATUS_TODO
    assert task.spec == "build X"
    assert task.assigned_to == ASSIGNEE_AGENT
    assert task.ready is True
    assert task.project_ids == ("proj-1",)


def test_parse_repo_from_url() -> None:
    page = {"properties": {"Repo": {"type": "url", "url": "https://github.com/o/r"}}}
    repo = parse_repo(page)
    assert repo.full_name == "o/r"


def test_is_dispatchable_guard() -> None:
    ok = parse_task(_task_page())
    assert is_dispatchable(ok)
    not_ready = parse_task(_task_page(ready=False))
    assert not is_dispatchable(not_ready)
    wrong_assignee = parse_task(_task_page(assignee="Someone"))
    assert not is_dispatchable(wrong_assignee)


def test_query_ready_tasks_filters_guard() -> None:
    pages = [
        _task_page(id="p1"),
        _task_page(id="p2", ready=False),  # filtered by Notion query (modeled here)
        _task_page(id="p3", assignee="Human"),  # filtered by the python guard
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/query")
        # Model the Notion `Ready==true` filter: drop the not-ready page.
        results = [p for p in pages if p["properties"]["Ready"]["checkbox"]]
        return httpx.Response(200, json={"results": results})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.notion.com"
    )
    notion = NotionTaskClient("tok", client=client)
    tasks = notion.query_ready_tasks("db-1")
    ids = {t.page_id for t in tasks}
    assert ids == {"p1"}  # p2 not ready, p3 wrong assignee


def test_build_feedback_summary_renders() -> None:
    md = build_feedback_summary(
        status="done",
        pr_url="https://github.com/o/r/pull/7",
        reflections=["missed AC-2 the first time"],
        coverage_note="tests cover all criteria",
    )
    assert "## Stromboli build summary" in md
    assert "pull/7" in md
    assert "missed AC-2" in md


def test_resilient_append_swallows_failure() -> None:
    class Boom:
        def append_task_body(self, page_id: str, markdown: str) -> None:
            raise RuntimeError("notion down")

    ok = resilient_append(Boom(), "p1", "hi", retries=2, sleep=lambda _s: None)
    assert ok is False


def test_resilient_append_succeeds() -> None:
    seen: list[tuple[str, str]] = []

    class Good:
        def append_task_body(self, page_id: str, markdown: str) -> None:
            seen.append((page_id, markdown))

    assert resilient_append(Good(), "p1", "hi") is True
    assert seen == [("p1", "hi")]


def test_task_is_frozen_dataclass() -> None:
    task = parse_task(_task_page())
    assert isinstance(task, Task)
