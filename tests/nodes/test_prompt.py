"""Tests for the prompting agent node (Spec → prompt → Notion)."""

from __future__ import annotations

from stromboli.nodes.prompt import make_prompt
from stromboli.state import Spec, StromboliState
from tests.nodes._fakes import FakeGateway, FakeNotion


def _state(source: str = "notion") -> StromboliState:
    return StromboliState(
        task_id="pg-1", source=source, raw_request="clean up the feeds",  # type: ignore[arg-type]
        spec=Spec(goal="dedupe feed sources", acceptance_criteria=["no dup urls"]),
    )


def test_stub_without_gateway_uses_spec_text() -> None:
    out = make_prompt()(_state())
    plan = out["plan"]
    assert isinstance(plan, str) and "dedupe feed sources" in plan


def test_gateway_generates_prompt_and_writes_to_notion() -> None:
    gw = FakeGateway()  # complete() → "generated coding prompt"
    notion = FakeNotion()
    out = make_prompt(gw, model="gemini-3.5-flash", notion=notion)(_state())
    assert out["plan"] == "generated coding prompt"
    # The generated prompt is written to the Notion "Prompt" field (traceable).
    assert notion.rich_text_writes == [("pg-1", "Prompt", "generated coding prompt")]
    assert gw.calls[0]["model"] == "gemini-3.5-flash"


def test_cli_source_does_not_write_to_notion() -> None:
    gw = FakeGateway()
    notion = FakeNotion()
    make_prompt(gw, model="m", notion=notion)(_state(source="cli"))
    assert notion.rich_text_writes == []


def test_gateway_failure_falls_back_to_spec_text() -> None:
    from stromboli.llm.gateway import GatewayError

    gw = FakeGateway(error=GatewayError("down"))
    out = make_prompt(gw, model="m", notion=FakeNotion())(_state())
    assert isinstance(out["plan"], str) and "dedupe feed sources" in out["plan"]


def test_planner_injects_retrieved_lessons() -> None:
    from stromboli.nodes.prompt import make_prompt
    from stromboli.state import Spec, StromboliState
    from tests.nodes._fakes import FakeGateway

    gw = FakeGateway({})
    lessons = ["When bugfix and off-by-one: fix the range bound."]
    node = make_prompt(gw, model="flash", retriever=lambda _g: lessons)
    node(StromboliState(
        task_id="t", source="cli", raw_request="fix the loop",
        spec=Spec(goal="fix the off-by-one loop"),
    ))
    assert "fix the range bound" in gw.calls[0]["user"]


def test_planner_without_retriever_injects_nothing() -> None:
    from stromboli.nodes.prompt import make_prompt
    from stromboli.state import Spec, StromboliState
    from tests.nodes._fakes import FakeGateway

    gw = FakeGateway({})
    node = make_prompt(gw, model="flash")
    node(StromboliState(
        task_id="t", source="cli", raw_request="do x", spec=Spec(goal="do x"),
    ))
    assert "Lessons from past" not in gw.calls[0]["user"]
