"""Tests for the project-context loader (Spec-node project conventions)."""

from __future__ import annotations

from typing import Any

from stromboli.integrations.project_context import (
    make_project_context,
    parse_blob_url,
)
from stromboli.state import StromboliState


def _state() -> StromboliState:
    return StromboliState(task_id="t1", source="cli", raw_request="add a flag")


def test_parse_blob_url() -> None:
    assert parse_blob_url("https://github.com/o/r/blob/main/README.md") == (
        "o", "r", "main", "README.md"
    )
    assert parse_blob_url("https://github.com/o/r/blob/dev/docs/a/b.md") == (
        "o", "r", "dev", "docs/a/b.md"
    )
    assert parse_blob_url("https://github.com/o/r") is None
    assert parse_blob_url("not a url") is None


class _Notion:
    """Minimal notion fake: a task with a project, and a Context Root URL."""

    def __init__(self, url: str | None) -> None:
        self._url = url
        self.lookups = 0

    def get_task(self, _task_id: str) -> object:
        return object()  # only passed back into get_project_context_url

    def get_project_context_url(self, _task: Any) -> str | None:
        self.lookups += 1
        return self._url


def test_loader_returns_labelled_block() -> None:
    notion = _Notion("https://github.com/o/r/blob/main/README.md")
    load = make_project_context(notion, fetch=lambda _u: "# Title\nUse framework Z")
    out = load(_state())
    assert "Project conventions (from https://github.com/o/r/blob/main/README.md)" in out
    assert "Use framework Z" in out


def test_loader_blank_url_returns_empty() -> None:
    load = make_project_context(_Notion(None), fetch=lambda _u: "should not be used")
    assert load(_state()) == ""


def test_loader_unresolvable_fetch_returns_empty() -> None:
    notion = _Notion("https://github.com/o/r/blob/main/MISSING.md")
    load = make_project_context(notion, fetch=lambda _u: None)  # 404 → None
    assert load(_state()) == ""


def test_loader_truncates_to_budget() -> None:
    notion = _Notion("https://github.com/o/r/blob/main/README.md")
    load = make_project_context(notion, fetch=lambda _u: "x" * 5000, budget=10)
    out = load(_state())
    assert out.count("x") == 10  # capped


def test_loader_caches_per_url() -> None:
    notion = _Notion("https://github.com/o/r/blob/main/README.md")
    calls = {"n": 0}

    def fetch(_url: str) -> str:
        calls["n"] += 1
        return "conventions"

    load = make_project_context(notion, fetch=fetch)
    load(_state())
    load(_state())
    assert calls["n"] == 1  # second call served from cache


def test_loader_swallows_notion_errors() -> None:
    class _Boom:
        def get_task(self, _task_id: str) -> object:
            raise RuntimeError("notion down")

    load = make_project_context(_Boom(), fetch=lambda _u: "x")
    assert load(_state()) == ""  # never blocks speccing
