"""Tests for :mod:`stromboli.notion`.

These exercise the pure parsers against mocked Notion API page payloads and the
HTTP client's outgoing requests via an ``httpx.MockTransport`` (no network).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from stromboli.notion import (
    STATUS_COMPLETE,
    STATUS_REVIEW,
    STATUS_TODO,
    STATUS_WORKING,
    NotionTaskClient,
    Repo,
    Task,
    parse_repo,
    parse_task,
)


def _task_page(**overrides: Any) -> dict[str, Any]:
    """A representative Notion *page* object for a task, with sane defaults."""
    props: dict[str, Any] = {
        "Task name": {
            "id": "title",
            "type": "title",
            "title": [{"type": "text", "plain_text": "Build the widget"}],
        },
        "Project": {
            "type": "relation",
            "relation": [{"id": "project-page-id-1"}],
        },
        "Status": {"type": "status", "status": {"name": "To do"}},
        "Spec": {
            "type": "rich_text",
            "rich_text": [
                {"plain_text": "Given a widget, "},
                {"plain_text": "when built, it works."},
            ],
        },
        "Assigned to": {"type": "select", "select": {"name": "Agent"}},
        "Ready": {"type": "checkbox", "checkbox": True},
        "Needs review": {"type": "checkbox", "checkbox": False},
        "PR": {"type": "url", "url": "https://github.com/o/r/pull/7"},
        "Cost": {"type": "number", "number": 1.23},
        "Tokens": {"type": "number", "number": 45000},
    }
    props.update(overrides.pop("properties", {}))
    page = {"object": "page", "id": "task-page-id", "properties": props}
    page.update(overrides)
    return page


def _project_page(repo_prop: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "page",
        "id": "project-page-id-1",
        "properties": {"Repo": repo_prop},
    }


# --------------------------------------------------------------------------- #
# parse_task                                                                   #
# --------------------------------------------------------------------------- #


def test_parse_task_extracts_every_field() -> None:
    task = parse_task(_task_page())

    assert isinstance(task, Task)
    assert task.page_id == "task-page-id"
    assert task.name == "Build the widget"
    assert task.project_ids == ("project-page-id-1",)
    assert task.status == "To do"
    assert task.spec == "Given a widget, when built, it works."
    assert task.assigned_to == "Agent"
    assert task.ready is True
    assert task.needs_review is False
    assert task.pr_url == "https://github.com/o/r/pull/7"
    assert task.cost == 1.23
    assert task.tokens == 45000


def test_parse_task_handles_empty_and_null_fields() -> None:
    page = _task_page(
        properties={
            "Task name": {"type": "title", "title": []},
            "Project": {"type": "relation", "relation": []},
            "Status": {"type": "status", "status": None},
            "Spec": {"type": "rich_text", "rich_text": []},
            "Assigned to": {"type": "select", "select": None},
            "PR": {"type": "url", "url": None},
            "Cost": {"type": "number", "number": None},
            "Tokens": {"type": "number", "number": None},
        }
    )

    task = parse_task(page)

    assert task.name == ""
    assert task.project_ids == ()
    assert task.status is None
    assert task.spec == ""
    assert task.assigned_to is None
    assert task.pr_url is None
    assert task.cost is None
    assert task.tokens is None
    # Missing checkbox payload defaults to False, not an error.
    assert task.ready is True  # still present in defaults
    assert task.needs_review is False


def test_parse_task_assigned_to_supports_people_property() -> None:
    page = _task_page(
        properties={
            "Assigned to": {
                "type": "people",
                "people": [{"id": "u1", "name": "Agent"}],
            }
        }
    )
    assert parse_task(page).assigned_to == "Agent"


# --------------------------------------------------------------------------- #
# parse_repo                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "repo_prop",
    [
        {"type": "url", "url": "https://github.com/snarktank/ralph"},
        {"type": "url", "url": "https://github.com/snarktank/ralph.git"},
        {"type": "url", "url": "git@github.com:snarktank/ralph.git"},
        {
            "type": "rich_text",
            "rich_text": [{"plain_text": "snarktank/ralph"}],
        },
        {
            "type": "rich_text",
            "rich_text": [{"plain_text": "https://github.com/snarktank/ralph/"}],
        },
    ],
)
def test_parse_repo_resolves_owner_and_repo(repo_prop: dict[str, Any]) -> None:
    repo = parse_repo(_project_page(repo_prop))

    assert repo == Repo(owner="snarktank", repo="ralph")
    assert repo.full_name == "snarktank/ralph"


def test_parse_repo_raises_when_repo_missing() -> None:
    page = {"object": "page", "id": "p", "properties": {}}
    with pytest.raises(ValueError, match="Repo"):
        parse_repo(page)


def test_parse_repo_raises_on_unparseable_value() -> None:
    page = _project_page({"type": "rich_text", "rich_text": [{"plain_text": "nope"}]})
    with pytest.raises(ValueError, match="owner/repo"):
        parse_repo(page)


# --------------------------------------------------------------------------- #
# HTTP client                                                                 #
# --------------------------------------------------------------------------- #


def _client(handler: httpx.MockTransport) -> NotionTaskClient:
    return NotionTaskClient(
        token="secret_notion",
        client=httpx.Client(
            transport=handler, base_url=NotionTaskClient.BASE_URL
        ),
    )


def test_get_task_fetches_and_parses_page() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["version"] = request.headers.get("notion-version")
        return httpx.Response(200, json=_task_page())

    client = _client(httpx.MockTransport(handler))
    task = client.get_task("task-page-id")

    assert task.name == "Build the widget"
    assert seen["url"].endswith("/pages/task-page-id")
    assert seen["auth"] == "Bearer secret_notion"
    assert seen["version"]  # a Notion-Version header is always sent


def test_get_project_repo_follows_relation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/pages/project-page-id-1"
        return httpx.Response(
            200,
            json=_project_page(
                {"type": "url", "url": "https://github.com/snarktank/ralph"}
            ),
        )

    client = _client(httpx.MockTransport(handler))
    task = parse_task(_task_page())

    assert client.get_project_repo(task) == Repo(owner="snarktank", repo="ralph")


def test_get_project_repo_raises_without_a_project() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    task = parse_task(_task_page(properties={"Project": {"type": "relation", "relation": []}}))

    with pytest.raises(ValueError, match="no Project"):
        client.get_project_repo(task)


def test_update_task_maps_status_to_exact_label_without_creating_options() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"object": "page", "id": "task-page-id"})

    client = _client(httpx.MockTransport(handler))
    client.update_task(
        "task-page-id",
        status=STATUS_WORKING,
        pr_url="https://github.com/o/r/pull/9",
        cost=2.5,
        tokens=1000,
    )

    assert captured["method"] == "PATCH"
    assert captured["path"] == "/v1/pages/task-page-id"
    props = captured["body"]["properties"]
    # Status sent by exact existing label name (no `options` create payload).
    assert props["Status"] == {"status": {"name": "Working on"}}
    assert "options" not in json.dumps(props)
    assert props["PR"] == {"url": "https://github.com/o/r/pull/9"}
    assert props["Cost"] == {"number": 2.5}
    assert props["Tokens"] == {"number": 1000}


def test_update_task_only_sends_provided_fields() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = _client(httpx.MockTransport(handler))
    client.update_task("task-page-id", status=STATUS_REVIEW)

    assert captured["body"]["properties"] == {"Status": {"status": {"name": "Review"}}}


def test_update_task_rejects_unknown_status() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ValueError, match="status"):
        client.update_task("task-page-id", status="Done")


def test_update_task_noop_when_nothing_to_write() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = _client(httpx.MockTransport(handler))
    client.update_task("task-page-id")

    assert called is False


def test_known_status_labels_are_the_exact_notion_options() -> None:
    assert STATUS_TODO == "To do"
    assert STATUS_WORKING == "Working on"
    assert STATUS_REVIEW == "Review"
    assert STATUS_COMPLETE == "Complete"


def test_append_task_body_posts_paragraph_blocks() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = _client(httpx.MockTransport(handler))
    client.append_task_body("task-page-id", "First line.\n\nSecond paragraph.")

    assert captured["method"] == "PATCH"
    assert captured["path"] == "/v1/blocks/task-page-id/children"
    blocks = captured["body"]["children"]
    assert len(blocks) == 2
    assert blocks[0]["type"] == "paragraph"
    texts = [
        b["paragraph"]["rich_text"][0]["text"]["content"] for b in blocks
    ]
    assert texts == ["First line.", "Second paragraph."]


def test_append_task_body_ignores_empty_markdown() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = _client(httpx.MockTransport(handler))
    client.append_task_body("task-page-id", "   \n\n  ")

    assert called is False
