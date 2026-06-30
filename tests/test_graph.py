"""Phase 0 — the stub graph runs end-to-end; edges route correctly."""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from stromboli.config import Budgets
from stromboli.graph import GraphDeps, build_graph, run_task
from stromboli.nodes.router import (
    make_route_after_verdict,
    route_after_coding,
    route_after_spec,
)
from stromboli.state import Spec, StromboliState, Verdict


def _offline_deps() -> GraphDeps:
    # NullTracer + default budgets => fully offline, no settings/Langfuse needed.
    return GraphDeps()


def test_durable_worktree_for_and_remover() -> None:
    from stromboli.graph import _durable_worktree_for, _worktree_remover
    from tests.nodes._fakes import FakeNotion, make_worktree

    calls: dict[str, list[tuple[str, str]]] = {"ensure": [], "remove": []}

    class _Mgr:
        def ensure(self, _repo: object, task_id: str, name: str) -> object:
            calls["ensure"].append((task_id, name))
            return make_worktree()

        def remove(self, _repo: object, task_id: str, name: str) -> None:
            calls["remove"].append((task_id, name))

    notion = FakeNotion()  # get_task → page-1/"Task"; get_project_repo → Repo
    mgr = _Mgr()
    state = StromboliState(task_id="page-1", source="notion", raw_request="x")
    _durable_worktree_for(mgr, notion)(state)
    _worktree_remover(mgr, notion)("page-1")
    assert calls["ensure"] == [("page-1", "Task")]
    assert calls["remove"] == [("page-1", "Task")]


def test_stub_run_reaches_done() -> None:
    final = run_task("stub", deps=_offline_deps(), checkpointer=MemorySaver())
    assert final.status == "done"
    assert final.spec is not None
    assert final.verdict is not None
    assert final.verdict.decision == "pass"
    # The coding stub appended exactly one passing test result (append reducer).
    assert len(final.test_results) == 1
    assert final.test_results[0].passed is True


def test_run_task_generates_task_id() -> None:
    final = run_task("stub", deps=_offline_deps())
    assert final.task_id  # a uuid hex was generated
    assert final.source == "cli"


def test_router_ambiguous_goes_to_human() -> None:
    state = StromboliState(
        task_id="t", source="cli", raw_request="vague",
        spec=Spec(goal="?", ambiguous=True),
    )
    assert route_after_spec(state) == "human"


def test_router_ready_goes_to_prompt() -> None:
    state = StromboliState(
        task_id="t", source="cli", raw_request="clear",
        spec=Spec(goal="do x", ambiguous=False),
    )
    # Ready spec → prompt agent (Spec→Prompt→Coding).
    assert route_after_spec(state) == "prompt"


def test_verdict_gate_pass_to_pr() -> None:
    gate = make_route_after_verdict(Budgets())
    state = StromboliState(
        task_id="t", source="cli", raw_request="x",
        verdict=Verdict(decision="pass", reason="ok"),
    )
    assert gate(state) == "pr"


def test_verdict_gate_revise_under_cap_to_coding() -> None:
    gate = make_route_after_verdict(Budgets(max_outer_revisions=3))
    state = StromboliState(
        task_id="t", source="cli", raw_request="x", outer_iterations=1,
        verdict=Verdict(decision="revise", reason="fix it"),
    )
    assert gate(state) == "coding"


def test_verdict_gate_revise_at_cap_escalates() -> None:
    gate = make_route_after_verdict(Budgets(max_outer_revisions=2))
    state = StromboliState(
        task_id="t", source="cli", raw_request="x", outer_iterations=2,
        verdict=Verdict(decision="revise", reason="still broken"),
    )
    assert gate(state) == "human"


def test_verdict_gate_escalate_to_human() -> None:
    gate = make_route_after_verdict(Budgets())
    state = StromboliState(
        task_id="t", source="cli", raw_request="x",
        verdict=Verdict(decision="escalate", reason="needs human"),
    )
    assert gate(state) == "human"


def test_route_after_coding_escalates_on_rate_limit() -> None:
    # Coding sets status=escalated on a rate-limit cutoff (PRD §4a) → human.
    escalated = StromboliState(
        task_id="t", source="cli", raw_request="x", status="escalated"
    )
    assert route_after_coding(escalated) == "human"
    # A normal coding pass proceeds to verification.
    normal = StromboliState(task_id="t", source="cli", raw_request="x", status="coding")
    assert route_after_coding(normal) == "verifier"


def test_unbuildable_task_escalates_not_crashes(tmp_path: object) -> None:
    from pathlib import Path

    from stromboli.graph import _escalate_unbuildable
    from stromboli.integrations.telegram import TelegramNotifier
    from tests.nodes._fakes import FakeNotion

    pushes: list[str] = []
    notion = FakeNotion()
    deps = GraphDeps(
        notion=notion,
        notifier=TelegramNotifier(send=pushes.append),
        workspace_root=Path(str(tmp_path)),
    )
    final = _escalate_unbuildable(deps, "pg-x", "do x", "Task has no Project relation")
    # Graceful: Review status + Notion note + Telegram, no exception.
    assert final.status == "escalated"
    assert ("pg-x", "Review") in notion.status_writes
    assert any("could not start" in md for _p, md in notion.appended)
    assert any("Escalation" in p for p in pushes)


def test_build_graph_is_compilable() -> None:
    graph = build_graph(_offline_deps(), checkpointer=MemorySaver())
    # The compiled graph exposes the canonical node set.
    nodes = set(graph.get_graph().nodes)
    for name in ("intake", "spec", "prompt", "coding", "verifier", "pr", "human",
                 "memory"):
        assert name in nodes
