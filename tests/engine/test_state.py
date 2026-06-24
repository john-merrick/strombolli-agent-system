"""Tests for the graph engine's build state (Phase 1).

State is plain data plus pure helpers and an atomic disk round-trip. These
assert the helpers (eligible/verified/blocked), the serialization round-trip
(including nested verdicts and unit phases), the atomic write (no ``.tmp`` left
behind, the sidecar ``feedback.md`` mirror, the ``transcripts/`` dir), and that
a state saved then loaded from disk resumes identically.
"""

from __future__ import annotations

from pathlib import Path

from stromboli.engine.state import (
    FEEDBACK_FILE,
    STATE_DIR,
    STATE_FILE,
    TRANSCRIPTS_DIR,
    BuildState,
    Plan,
    Unit,
    UnitPhase,
    UnitStatus,
    Verdict,
)


def _unit(uid: str, **overrides: object) -> Unit:
    base: dict[str, object] = {
        "id": uid,
        "description": f"do {uid}",
        "acceptance_criterion": f"{uid} works",
    }
    base.update(overrides)
    return Unit(**base)  # type: ignore[arg-type]


def _state(tmp_path: Path, units: list[Unit], **overrides: object) -> BuildState:
    base: dict[str, object] = {"plan": Plan(version=1, units=units), "root": tmp_path}
    base.update(overrides)
    return BuildState(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def test_initial_state_is_plan_less(tmp_path: Path) -> None:
    state = BuildState.initial(tmp_path)
    assert state.plan.is_empty
    assert state.next_eligible_unit() is None
    assert state.all_verified() is False


def test_next_eligible_is_first_pending(tmp_path: Path) -> None:
    state = _state(
        tmp_path,
        [
            _unit("a", status=UnitStatus.VERIFIED),
            _unit("b"),
            _unit("c"),
        ],
    )
    eligible = state.next_eligible_unit()
    assert eligible is not None and eligible.id == "b"


def test_all_verified_requires_every_unit(tmp_path: Path) -> None:
    assert _state(tmp_path, [_unit("a", status=UnitStatus.VERIFIED)]).all_verified()
    mixed = _state(
        tmp_path,
        [_unit("a", status=UnitStatus.VERIFIED), _unit("b")],
    )
    assert mixed.all_verified() is False


def test_blocked_units_filters_blocked(tmp_path: Path) -> None:
    state = _state(
        tmp_path,
        [_unit("a", status=UnitStatus.BLOCKED), _unit("b")],
    )
    assert [u.id for u in state.blocked_units()] == ["a"]


def test_unit_lookup_by_id(tmp_path: Path) -> None:
    state = _state(tmp_path, [_unit("a"), _unit("b")])
    found = state.unit("b")
    assert found is not None and found.id == "b"
    assert state.unit("missing") is None


def test_record_appends_history(tmp_path: Path) -> None:
    state = _state(tmp_path, [_unit("a")])
    state.record({"action": "RunWorker", "unit": "a"})
    assert state.history == [{"action": "RunWorker", "unit": "a"}]


# --------------------------------------------------------------------------- #
# Serialization round-trip                                                     #
# --------------------------------------------------------------------------- #
def test_save_load_round_trip_preserves_everything(tmp_path: Path) -> None:
    verdict = Verdict(scope="a", met=False, hollow_tests=True, reasons=("nope",))
    unit = _unit(
        "a",
        attempts=2,
        phase=UnitPhase.REFLECT,
        last_verdict=verdict,
        diff_hashes=("h1", "h2"),
    )
    state = _state(
        tmp_path,
        [unit, _unit("b", status=UnitStatus.VERIFIED)],
        feedback="be better",
        replan_count=1,
        whole_verdict=Verdict(scope="whole-diff", met=True),
        pending_replan="too big",
        breaker_tripped=True,
    )
    state.record({"step": 1})
    state.save()

    loaded = BuildState.load(tmp_path)
    assert loaded.plan.version == 1
    assert loaded.feedback == "be better"
    assert loaded.replan_count == 1
    assert loaded.pending_replan == "too big"
    assert loaded.breaker_tripped is True
    assert loaded.whole_verdict == Verdict(scope="whole-diff", met=True)
    assert loaded.history == [{"step": 1}]

    a = loaded.unit("a")
    assert a is not None
    assert a.attempts == 2
    assert a.phase is UnitPhase.REFLECT
    assert a.last_verdict == verdict
    assert a.diff_hashes == ("h1", "h2")
    b = loaded.unit("b")
    assert b is not None and b.status is UnitStatus.VERIFIED


def test_save_is_atomic_and_writes_sidecars(tmp_path: Path) -> None:
    state = _state(tmp_path, [_unit("a")], feedback="hello feedback")
    target = state.save()

    state_dir = tmp_path / STATE_DIR
    assert target == state_dir / STATE_FILE
    assert target.exists()
    # No temp file is left behind by the atomic rename.
    assert not (state_dir / f"{STATE_FILE}.tmp").exists()
    # Sidecars exist.
    assert (state_dir / FEEDBACK_FILE).read_text(encoding="utf-8") == "hello feedback"
    assert (state_dir / TRANSCRIPTS_DIR).is_dir()


def test_save_overwrites_existing_state(tmp_path: Path) -> None:
    _state(tmp_path, [_unit("a")], replan_count=0).save()
    _state(tmp_path, [_unit("a")], replan_count=5).save()
    assert BuildState.load(tmp_path).replan_count == 5


def test_plan_is_empty_for_version_zero_or_no_units(tmp_path: Path) -> None:
    assert Plan(version=0, units=[_unit("a")]).is_empty
    assert Plan(version=1, units=[]).is_empty
    assert not Plan(version=1, units=[_unit("a")]).is_empty
