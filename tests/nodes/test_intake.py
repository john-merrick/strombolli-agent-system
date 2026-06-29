"""Tests for the Intake node (PRD §6.1)."""

from __future__ import annotations

from stromboli.nodes.intake import make_intake
from stromboli.state import StromboliState
from tests.nodes._fakes import FakeNotion, make_task


def test_cli_intake_passes_through() -> None:
    state = StromboliState(task_id="t", source="cli", raw_request="do the thing")
    out = make_intake()(state)
    assert out == {"status": "intake"}


def test_notion_intake_hydrates_raw_request() -> None:
    notion = FakeNotion(make_task(page_id="pg", spec="implement the widget"))
    state = StromboliState(task_id="pg", source="notion", raw_request="")
    out = make_intake(notion=notion)(state)
    assert out["raw_request"] == "implement the widget"
    assert out["status"] == "intake"


def test_notion_intake_falls_back_to_name() -> None:
    notion = FakeNotion(make_task(page_id="pg", spec="   ", name="Fix the bug"))
    state = StromboliState(task_id="pg", source="notion", raw_request="")
    out = make_intake(notion=notion)(state)
    assert out["raw_request"] == "Fix the bug"
