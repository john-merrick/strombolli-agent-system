"""Phase 3 — the outer recursion: revise loops back, then escalates at the cap."""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from stromboli.config import Budgets
from stromboli.graph import GraphDeps, build_graph
from stromboli.llm.coder import CoderRun, TurnRecord
from stromboli.sandbox.runner import SandboxResult
from stromboli.state import StromboliState
from tests.nodes._fakes import RoutingGateway, make_worktree


class _Coder:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def run(self, prompt: str, cwd: str | Path, *, resume: str | None = None) -> CoderRun:
        self.calls.append(resume)
        return CoderRun(
            diff="diff --git a/x b/x\n+change",
            final_text="done",
            turns=2,
            session_id="sess-1",
            subtype="success",
            is_error=False,
            cost_usd=0.01,
            usage=None,
            turn_records=(TurnRecord(index=1, tools=("Edit",), usage=None),),
        )


class _Sandbox:
    def run_tests(self, worktree_path: str | Path, command: object = None) -> SandboxResult:
        return SandboxResult(passed=True, output="1 passed", exit_code=0)


def _always_revise_deps(coder: _Coder, cap: int) -> GraphDeps:
    gateway = RoutingGateway(
        {
            "Spec": {"goal": "do x", "acceptance_criteria": ["x works"],
                     "ambiguous": False},
            "Verdict": {"decision": "revise", "reason": "still missing x"},
        }
    )
    return GraphDeps(
        budgets=Budgets(max_outer_revisions=cap),
        gateway=gateway,
        reasoning_model="haiku",
        verifier_model="gemini/gemini-2.5-pro",
        coder=coder,
        sandbox=_Sandbox(),
        worktree_for=lambda _s: make_worktree(),
    )


def test_revise_loops_back_then_escalates_at_cap() -> None:
    coder = _Coder()
    cap = 2
    graph = build_graph(_always_revise_deps(coder, cap), checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "loop-1"}}

    result = graph.invoke(
        StromboliState(task_id="loop-1", source="cli", raw_request="do x"),
        config=config,
    )
    # The loop terminated by escalating to the Human Interrupt (which pauses).
    assert "__interrupt__" in result
    snapshot = graph.get_state(config)
    assert snapshot.next == ("human",)

    state = snapshot.values
    # Bounded: cap revise re-codes were taken, then it escalated (no spin).
    assert state["outer_iterations"] == cap
    # Coder ran the initial pass + one per revise cycle, all resuming the session.
    assert len(coder.calls) == cap + 1
    assert coder.calls[1] == "sess-1"  # revise passes resume the SDK session
    # A reflection was recorded for every non-pass verdict.
    assert len(state["reflections"]) == cap + 1


def test_pass_on_first_try_reaches_pr_then_done() -> None:
    coder = _Coder()
    gateway = RoutingGateway(
        {
            "Spec": {"goal": "do x", "acceptance_criteria": ["x"], "ambiguous": False},
            "Verdict": {"decision": "pass", "reason": "looks right"},
        }
    )
    deps = GraphDeps(
        gateway=gateway, reasoning_model="haiku", verifier_model="gemini/x",
        coder=coder, sandbox=_Sandbox(), worktree_for=lambda _s: make_worktree(),
    )
    graph = build_graph(deps, checkpointer=MemorySaver())
    final = graph.invoke(
        StromboliState(task_id="ok-1", source="cli", raw_request="do x"),
        config={"configurable": {"thread_id": "ok-1"}},
    )
    assert final["status"] == "done"
    assert len(coder.calls) == 1
