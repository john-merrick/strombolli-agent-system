"""Tests for the Notion acknowledgment listener."""

from __future__ import annotations

import pytest

from stromboli.ledger import RunRecord, RunState
from stromboli.notify import NotionAck, render_building, render_queued


def _run(**overrides: object) -> RunRecord:
    base: dict[str, object] = {
        "id": 7,
        "page_id": "page-1",
        "task_name": "Add healthcheck",
        "engine": "graph",
        "state": RunState.QUEUED,
        "stage": None,
        "outcome": None,
        "error": None,
        "queued_at": "2026-06-29T12:00:00+00:00",
        "started_at": None,
        "ended_at": None,
        "heartbeat_at": None,
    }
    base.update(overrides)
    return RunRecord(**base)  # type: ignore[arg-type]


class FakeNotion:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str]] = []

    def append_task_body(self, page_id: str, markdown: str) -> None:
        self.appended.append((page_id, markdown))


def test_render_queued_shows_next_up_for_head() -> None:
    note = render_queued(_run(), position=0)
    assert "next up" in note
    assert "2026-06-29T12:00:00+00:00" in note


def test_render_queued_shows_place_in_line() -> None:
    note = render_queued(_run(), position=2)
    assert "#3 in the queue" in note


def test_render_building_names_the_run() -> None:
    assert "run 7" in render_building(_run())


def test_ack_appends_queued_note_with_position() -> None:
    notion = FakeNotion()
    NotionAck(notion).queued(_run(), position=1)
    assert len(notion.appended) == 1
    page_id, note = notion.appended[0]
    assert page_id == "page-1"
    assert "#2 in the queue" in note


def test_ack_appends_building_note() -> None:
    notion = FakeNotion()
    NotionAck(notion).building(_run())
    assert notion.appended[0][0] == "page-1"
    assert "building" in notion.appended[0][1].lower()


def test_ack_finished_is_a_noop_to_avoid_clutter() -> None:
    notion = FakeNotion()
    NotionAck(notion).finished(_run(state=RunState.DONE))
    assert notion.appended == []


def test_ack_never_raises_on_notion_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stromboli.writeback.time.sleep", lambda _: None)

    class BrokenNotion:
        def append_task_body(self, page_id: str, markdown: str) -> None:
            raise RuntimeError("notion down")

    # resilient_append swallows the failure — the ack must not propagate it.
    NotionAck(BrokenNotion()).queued(_run(), position=0)
