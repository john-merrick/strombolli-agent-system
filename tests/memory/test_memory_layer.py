"""Tests for the ChromaDB three-tier memory (PRD §7)."""

from __future__ import annotations

from stromboli.memory import Memory
from stromboli.memory.store import EPISODIC, PROCEDURAL
from tests.memory._fakes import make_store


def _memory() -> Memory:
    return Memory(make_store())


def test_episodic_recall_finds_relevant_reflection() -> None:
    mem = _memory()
    mem.episodic.record_reflection(
        "task-1", "remember to add a regression test for pagination bugs", ts=1.0
    )
    mem.episodic.record_reflection(
        "task-2", "the deployment script needs the staging flag", ts=2.0
    )
    hits = mem.episodic.recall("pagination regression test", k=1)
    assert hits
    assert "pagination" in hits[0].document


def test_recall_for_spec_pulls_semantic_conventions() -> None:
    # Spec recall now carries only repo conventions; distilled lessons moved to
    # the planner (recall_lessons). See docs/design-context-as-state.md.
    mem = _memory()
    mem.semantic.add_convention("style", "always use snake_case for functions")
    snippets = mem.recall_for_spec("naming style", k=2)
    assert any("snake_case" in s for s in snippets)


def test_recall_lessons_filters_to_lesson_kind() -> None:
    mem = _memory()
    mem.episodic.record_lesson(
        "t1", "When bugfix and off-by-one: fix the range bound.",
        task_type="bugfix", failure_mode="off-by-one", ts=1.0,
    )
    # A trace on the same topic must NOT surface via recall_lessons.
    mem.episodic.record_trace("t2", "shipped an off-by-one range change", ts=2.0)
    lessons = mem.recall_lessons("off by one range", k=3)
    assert any("fix the range bound" in lesson for lesson in lessons)
    assert not any("shipped" in lesson for lesson in lessons)


def test_recall_is_bounded_by_k() -> None:
    mem = _memory()
    for i in range(10):
        mem.episodic.record_reflection(f"t{i}", f"lesson number {i} about widgets", ts=float(i))
    hits = mem.episodic.recall("widgets lesson", k=3)
    assert len(hits) <= 3  # retrieve, don't accumulate


def test_metadata_carries_kind_and_ts() -> None:
    mem = _memory()
    mem.episodic.record_trace("t1", "shipped the widget", ts=5.0)
    hit = mem.episodic.recall("widget shipped", k=1)[0]
    assert hit.metadata["kind"] == "trace"
    assert hit.metadata["ts"] == 5.0
    assert hit.metadata["tier"] == EPISODIC


def test_procedural_skill_roundtrip() -> None:
    mem = _memory()
    mem.procedural.add_skill(
        "retry", "def retry(fn): ... # exponential backoff", task_id="t1", ts=1.0
    )
    hits = mem.procedural.recall("retry backoff", k=1)
    assert hits and hits[0].metadata["tier"] == PROCEDURAL


def test_upsert_overwrites_same_id() -> None:
    store = make_store()
    store.add(EPISODIC, id="x", document="first", metadata={"ts": 1.0})
    store.add(EPISODIC, id="x", document="second version", metadata={"ts": 2.0})
    hits = store.query(EPISODIC, text="second version", k=5)
    assert len([h for h in hits if h.id == "x"]) == 1


def test_skill_candidate_not_recalled_until_approved() -> None:
    from stromboli.memory.procedural import STATUS_APPROVED
    mem = _memory()
    mem.procedural.add_skill(
        "s1", "For add-endpoint: wire the route and add a test.",
        task_id="t1", ts=1.0, task_type="add-endpoint",
    )
    # A candidate is invisible to the planner (recall_skills = approved only).
    assert mem.recall_skills("add an endpoint", k=3) == []
    # Promote it, then it is injected.
    assert mem.procedural.promote("s1", ts=2.0) is True
    got = mem.recall_skills("add an endpoint route", k=3)
    assert any("wire the route" in s for s in got)
    hit = mem.procedural.recall("add endpoint", k=1)[0]
    assert hit.metadata["status"] == STATUS_APPROVED


def test_promote_missing_skill_is_false() -> None:
    mem = _memory()
    assert mem.procedural.promote("nope", ts=1.0) is False
