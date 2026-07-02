"""Tests for the Memory Write node + the loop-closer (PRD §6.9 / §7)."""

from __future__ import annotations

from stromboli.memory import Memory
from stromboli.memory.store import EPISODIC
from stromboli.nodes.memory import make_memory_write
from stromboli.state import Spec, StromboliState, Verdict
from tests.memory._fakes import make_store
from tests.nodes._fakes import FakeGateway


def _passed_state(**over: object) -> StromboliState:
    base: dict[str, object] = dict(
        task_id="task-1", source="cli", raw_request="add pagination",
        spec=Spec(goal="add pagination"),
        verdict=Verdict(decision="pass", reason="ok"),
        reflections=["revise: first attempt missed the empty-page case"],
    )
    base.update(over)
    return StromboliState(**base)  # type: ignore[arg-type]


def test_pass_deposits_episodic_trace_and_reflections() -> None:
    mem = Memory(make_store())
    out = make_memory_write(mem, now=lambda: 1.0)(_passed_state())
    assert out["status"] == "done"
    # One trace + one reflection were written to episodic memory.
    hits = mem.episodic.recall("pagination empty-page", k=5)
    kinds = {h.metadata["kind"] for h in hits}
    assert "trace" in kinds
    assert "reflection" in kinds


def test_noop_without_memory() -> None:
    out = make_memory_write(None)(_passed_state())
    assert out == {"status": "done"}


def test_escalation_writes_only_reflections() -> None:
    mem = Memory(make_store())
    state = _passed_state(verdict=Verdict(decision="escalate", reason="stuck"))
    make_memory_write(mem, now=lambda: 2.0)(state)
    hits = mem.episodic.recall("missed empty-page case", k=5)
    kinds = {h.metadata["kind"] for h in hits}
    assert "reflection" in kinds
    assert "trace" not in kinds  # no trace on a non-pass


def test_loop_closer_planner_retrieves_planted_lesson() -> None:
    # Plant a distilled lesson from a prior task, then run the planner (prompt
    # node) on a similar request — the loop-closer now lands at the planner.
    from stromboli.nodes.prompt import make_prompt
    from stromboli.state import Spec

    mem = Memory(make_store())
    mem.episodic.record_lesson(
        "prior", "When add-pagination and missing-tests: add a test for the "
        "empty pagination page.",
        task_type="add-pagination", failure_mode="missing-tests", ts=1.0,
    )
    gateway = FakeGateway({"goal": "add pagination", "ambiguous": False})
    node = make_prompt(gateway, model="flash", retriever=mem.recall_lessons)
    state = StromboliState(
        task_id="new", source="cli", raw_request="add pagination to the list",
        spec=Spec(goal="add pagination to the list"),
    )
    node(state)
    # The planted lesson was retrieved and injected into the planner's prompt.
    assert "empty pagination page" in gateway.calls[0]["user"]


def test_store_tier_isolation() -> None:
    store = make_store()
    store.add(EPISODIC, id="e1", document="episodic note", metadata={"ts": 1.0})
    # Querying procedural returns nothing planted in episodic.
    assert store.query("procedural", text="episodic note", k=5) == []


def test_resolved_with_divergence_writes_a_lesson() -> None:
    mem = Memory(make_store())
    verdict = Verdict(
        decision="pass", reason="ok now",
        expected="a test for the empty page", observed="no such test initially",
        cause="forgot the empty-collection edge", fix="add an empty-page test",
        task_type="add-pagination", failure_mode="missing-tests",
    )
    make_memory_write(mem, now=lambda: 3.0)(_passed_state(verdict=verdict))
    lessons = mem.recall_lessons("pagination empty page test", k=3)
    assert any("add an empty-page test" in lesson for lesson in lessons)
    # And it is tagged as a lesson with its metadata.
    hit = mem.episodic.recall_lessons("empty page test", k=1)[0]
    assert hit.metadata["kind"] == "lesson"
    assert hit.metadata["task_type"] == "add-pagination"
    assert hit.metadata["failure_mode"] == "missing-tests"


def test_clean_pass_writes_no_lesson() -> None:
    # A first-pass pass with no divergence (empty surprise) → nothing learned.
    mem = Memory(make_store())
    make_memory_write(mem, now=lambda: 4.0)(
        _passed_state(verdict=Verdict(decision="pass", reason="clean"), reflections=[])
    )
    assert mem.recall_lessons("anything", k=5) == []


def test_escalation_writes_no_lesson() -> None:
    # No validated fix on a non-pass → no durable lesson (poisoning guard).
    mem = Memory(make_store())
    verdict = Verdict(
        decision="escalate", reason="stuck", cause="unclear", fix="ask a human",
        task_type="bugfix", failure_mode="ambiguous",
    )
    make_memory_write(mem, now=lambda: 5.0)(_passed_state(verdict=verdict))
    assert mem.recall_lessons("bugfix ambiguous", k=5) == []


def test_resolved_with_fix_writes_a_skill_candidate() -> None:
    from stromboli.memory.procedural import STATUS_CANDIDATE
    mem = Memory(make_store())
    verdict = Verdict(
        decision="pass", reason="ok", cause="forgot the test",
        fix="add an empty-page test", task_type="add-pagination",
        failure_mode="missing-tests",
    )
    make_memory_write(mem, now=lambda: 3.0)(_passed_state(verdict=verdict))
    # Skill is written but as an unvetted candidate → not recalled by planner.
    assert mem.recall_skills("pagination", k=3) == []
    hit = mem.procedural.recall("pagination empty page", k=1)[0]
    assert hit.metadata["status"] == STATUS_CANDIDATE
    assert "add an empty-page test" in hit.document


def test_pass_without_fix_writes_no_skill() -> None:
    mem = Memory(make_store())
    make_memory_write(mem, now=lambda: 4.0)(
        _passed_state(verdict=Verdict(decision="pass", reason="clean"), reflections=[])
    )
    assert mem.procedural.recall("anything", k=5) == []
