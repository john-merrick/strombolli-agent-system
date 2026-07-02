"""Tests for the skill A/B eval gate (self-improving §3)."""

from __future__ import annotations

from stromboli.memory import Memory
from stromboli.observability.evals.skill_gate import gate_skill
from tests.memory._fakes import make_store


def _mem_with_candidate() -> Memory:
    mem = Memory(make_store())
    mem.procedural.add_skill("s1", "some skill", task_id="t1", ts=1.0)
    return mem


def test_gate_promotes_when_no_regression() -> None:
    mem = _mem_with_candidate()
    # Candidate arm passes everything; baseline passes everything → promote.
    res = gate_skill(mem.procedural, "s1", lambda inp, cand: True, now=lambda: 2.0)
    assert res.promoted is True
    assert res.candidate_score >= res.baseline_score
    assert mem.recall_skills("some skill", k=3)  # now injectable


def test_gate_rejects_a_regressing_skill() -> None:
    mem = _mem_with_candidate()
    # Candidate arm fails the tasks; baseline passes → regression → reject.
    res = gate_skill(
        mem.procedural, "s1",
        lambda inp, cand: not cand,  # passes only when candidate is off
        now=lambda: 2.0,
    )
    assert res.promoted is False
    assert mem.recall_skills("some skill", k=3) == []  # stays a candidate
