"""Tests for the Memory Write node + the loop-closer (PRD §6.9 / §7)."""

from __future__ import annotations

from stromboli.memory import Memory
from stromboli.memory.store import EPISODIC
from stromboli.nodes.memory import make_memory_write
from stromboli.nodes.spec import make_spec
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


def test_loop_closer_spec_retrieves_planted_reflection() -> None:
    # Plant a reflection from a prior task, then run Spec on a similar request.
    mem = Memory(make_store())
    mem.episodic.record_reflection(
        "prior", "always add a test for the empty pagination page", ts=1.0
    )
    gateway = FakeGateway({"goal": "add pagination", "ambiguous": False})
    node = make_spec(gateway, model="haiku", retriever=mem.recall_for_spec)
    node(StromboliState(task_id="new", source="cli", raw_request="add pagination to the list"))
    # The planted reflection was retrieved and injected into the Spec prompt.
    assert "empty pagination page" in gateway.calls[0]["user"]


def test_store_tier_isolation() -> None:
    store = make_store()
    store.add(EPISODIC, id="e1", document="episodic note", metadata={"ts": 1.0})
    # Querying procedural returns nothing planted in episodic.
    assert store.query("procedural", text="episodic note", k=5) == []
