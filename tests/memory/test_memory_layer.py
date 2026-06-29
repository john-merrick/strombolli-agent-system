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


def test_recall_for_spec_pulls_episodic_and_semantic() -> None:
    mem = _memory()
    mem.episodic.record_reflection("t1", "watch out for off by one in ranges", ts=1.0)
    mem.semantic.add_convention("style", "always use snake_case for functions")
    snippets = mem.recall_for_spec("range off by one", k=2)
    assert any("off by one" in s for s in snippets)


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
