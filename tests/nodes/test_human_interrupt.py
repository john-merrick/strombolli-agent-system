"""Phase 1 — the graph pauses at the Human Interrupt and resumes (PRD §6.8)."""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from stromboli.graph import GraphDeps, build_graph
from stromboli.integrations.telegram import NullNotifier, TelegramNotifier
from stromboli.state import StromboliState
from tests.nodes._fakes import FakeGateway, FakeNotion


def _ambiguous_deps() -> tuple[GraphDeps, list[str], FakeNotion]:
    pushes: list[str] = []
    notifier = TelegramNotifier(send=pushes.append)
    notion = FakeNotion()
    deps = GraphDeps(
        gateway=FakeGateway({"goal": "unclear", "ambiguous": True}),
        reasoning_model="haiku",
        notifier=notifier,
        notion=notion,
    )
    return deps, pushes, notion


def test_graph_pauses_at_human_interrupt() -> None:
    deps, pushes, notion = _ambiguous_deps()
    graph = build_graph(deps, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "task-1"}}

    result = graph.invoke(
        StromboliState(task_id="task-1", source="cli", raw_request="vague"),
        config=config,
    )
    # The run paused at the interrupt rather than reaching a terminal state.
    assert "__interrupt__" in result
    # The escalation was surfaced to Telegram + Notion before pausing.
    assert any("Escalation" in p for p in pushes)
    assert notion.appended and "needs you" in notion.appended[0][1]

    # The checkpointer holds the paused state; the next node is the human node.
    snapshot = graph.get_state(config)
    assert snapshot.next == ("human",)


def test_graph_resumes_after_human_input() -> None:
    deps, _pushes, _notion = _ambiguous_deps()
    graph = build_graph(deps, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "task-2"}}

    graph.invoke(
        StromboliState(task_id="task-2", source="cli", raw_request="vague"),
        config=config,
    )
    # Resume with the human marking it resolved.
    final = graph.invoke(Command(resume={"action": "resolved"}), config=config)
    assert final["status"] == "done"
    # And the graph has no further work queued.
    assert graph.get_state(config).next == ()


def test_default_notifier_is_null() -> None:
    assert isinstance(GraphDeps().notifier, NullNotifier)
