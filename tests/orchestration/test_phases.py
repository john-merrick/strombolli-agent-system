"""Tests for the shared orchestrator-agnostic triage phases."""

from __future__ import annotations

from stromboli.graph import GraphDeps
from stromboli.integrations.telegram import TelegramNotifier
from stromboli.orchestration.phases import TriagePhases
from stromboli.state import Spec, StromboliState, Verdict
from tests.nodes._fakes import FakeNotion, RoutingGateway


def _state(source: str = "cli") -> StromboliState:
    return StromboliState(task_id="t1", source=source, raw_request="add a flag")  # type: ignore[arg-type]


def test_stub_happy_path_runs_to_done() -> None:
    p = TriagePhases(GraphDeps())
    s = _state()
    s = p.intake(s)
    s = p.spec(s)
    assert not p.is_ambiguous(s)
    s = p.prompt(s)
    assert s.plan  # the generated (stub) coding prompt
    s = p.coding(s)
    assert len(s.test_results) == 1 and s.test_results[0].passed is True
    s = p.verify(s)
    assert p.verdict_route(s) == "pr"
    s = p.open_pr(s)
    s = p.memory_write(s)
    assert s.status == "done"


def test_append_reducers_accumulate() -> None:
    p = TriagePhases(GraphDeps())
    s = p.coding(_state())          # appends 1 test result
    s = p.coding(s)                 # appends another
    assert len(s.test_results) == 2


def test_is_ambiguous_and_verdict_route_via_gateway() -> None:
    gw = RoutingGateway(
        {"Spec": {"goal": "?", "ambiguous": True},
         "Verdict": {"decision": "revise", "reason": "weak"}}
    )
    p = TriagePhases(GraphDeps(gateway=gw, reasoning_model="m", verifier_model="g"))
    s = p.spec(_state())
    assert p.is_ambiguous(s) is True


def test_verdict_route_revise_then_cap() -> None:
    from stromboli.config import Budgets
    p = TriagePhases(GraphDeps(budgets=Budgets(max_outer_revisions=1)))
    revise = StromboliState(
        task_id="t", source="cli", raw_request="x",
        verdict=Verdict(decision="revise", reason="fix"), outer_iterations=0,
    )
    assert p.verdict_route(revise) == "coding"
    revise_at_cap = revise.model_copy(update={"outer_iterations": 1})
    assert p.verdict_route(revise_at_cap) == "escalate"


def test_mark_escalated() -> None:
    p = TriagePhases(GraphDeps())
    s = p.mark_escalated(_state(), "spec is ambiguous")
    assert s.status == "escalated" and "ambiguous" in s.reflections[-1]


def test_suspend_persists_state_and_sets_queued(tmp_path: object) -> None:
    from pathlib import Path

    from stromboli.orchestration.paused import PausedIndex

    notion = FakeNotion()
    index = PausedIndex(Path(str(tmp_path)) / "paused.db")
    p = TriagePhases(GraphDeps(notion=notion, paused_index=index))
    s = StromboliState(
        task_id="pg-9", source="notion", raw_request="x",
        session_id="sess-1", spec=Spec(goal="g"),
    )
    out = p.suspend(s, "verifier rejected")

    assert out.status == "queued"
    assert "verifier rejected" in out.reflections[-1]
    assert ("pg-9", "Queued") in notion.status_writes
    row = index.get("pg-9")
    assert row is not None and row.ref == 1 and row.session_id == "sess-1"
    restored = index.load_state("pg-9")
    assert restored is not None and restored.status == "queued"


def test_resume_with_guidance_passes_to_done() -> None:
    p = TriagePhases(GraphDeps())  # stub coding + verify → happy path
    s = StromboliState(
        task_id="a", source="cli", raw_request="x", plan="original prompt",
        status="queued",
    )
    out = p.resume_with_guidance(s, "use library X")
    assert out.status == "done"
    assert out.plan is not None and "use library X" in out.plan
    assert any("resume guidance" in r for r in out.reflections)


def test_resume_with_guidance_failure_escalates_not_requeues() -> None:
    gw = RoutingGateway({"Verdict": {"decision": "revise", "reason": "still broken"}})
    p = TriagePhases(GraphDeps(gateway=gw, verifier_model="g"))
    s = StromboliState(task_id="a", source="cli", raw_request="x", status="queued")
    out = p.resume_with_guidance(s, "use library X")
    assert out.status == "escalated"  # → Review, not another Queued cycle
    assert "still broken" in out.reflections[-1]


def test_suspend_sends_investigate_opener(tmp_path: object) -> None:
    from pathlib import Path

    from stromboli.orchestration.paused import PausedIndex

    openers: list[str] = []
    index = PausedIndex(Path(str(tmp_path)) / "paused.db")
    p = TriagePhases(GraphDeps(paused_index=index, investigate_notify=openers.append))
    s = StromboliState(
        task_id="pg-1", source="notion", raw_request="x", spec=Spec(goal="add a flag")
    )
    p.suspend(s, "verifier rejected")
    assert openers and "#1" in openers[0]
    assert "add a flag" in openers[0] and "verifier rejected" in openers[0]


def test_finalize_writes_complete_and_telegram() -> None:
    pushes: list[str] = []
    notion = FakeNotion()
    p = TriagePhases(
        GraphDeps(notion=notion, notifier=TelegramNotifier(send=pushes.append))
    )
    done = StromboliState(
        task_id="pg-1", source="notion", raw_request="x", status="done",
        pr_url="https://pr/1", spec=Spec(goal="g"),
        verdict=Verdict(decision="pass", reason="ok"),
    )
    p.finalize(done)
    assert ("pg-1", "Complete") in notion.status_writes
    assert any("build summary" in md for _p, md in notion.appended)
    assert any("Done" in x for x in pushes)


def test_finalize_escalated_writes_review() -> None:
    pushes: list[str] = []
    notion = FakeNotion()
    p = TriagePhases(
        GraphDeps(notion=notion, notifier=TelegramNotifier(send=pushes.append))
    )
    esc = StromboliState(
        task_id="pg-2", source="notion", raw_request="x", status="escalated",
        reflections=["spec is ambiguous"],
    )
    p.finalize(esc)
    assert ("pg-2", "Review") in notion.status_writes
    assert any("Escalation" in x for x in pushes)
