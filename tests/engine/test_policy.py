"""Exhaustive table tests for the deterministic control policy (Phase 1).

The policy is the engine's brain and is pure, so every branch is asserted here
directly — no agents, no orchestrator. Each test pins one rule from the
precedence ladder in the policy docstring, covering every action the policy can
emit and every reason it can route to Review for.
"""

from __future__ import annotations

from pathlib import Path

from stromboli.engine.actions import (
    Integrate,
    Reflect,
    Replan,
    ReviewReason,
    RouteToReview,
    RunPlanner,
    RunVerifier,
    RunWorker,
)
from stromboli.engine.policy import DeterministicPolicy
from stromboli.engine.state import (
    WHOLE_DIFF,
    BuildState,
    Plan,
    Unit,
    UnitPhase,
    UnitStatus,
    Verdict,
)

ROOT = Path("/tmp/does-not-matter")
POLICY = DeterministicPolicy(max_attempts=3, max_replans=2)


def _unit(uid: str = "a", **overrides: object) -> Unit:
    base: dict[str, object] = {
        "id": uid,
        "description": f"do {uid}",
        "acceptance_criterion": f"{uid} works",
    }
    base.update(overrides)
    return Unit(**base)  # type: ignore[arg-type]


def _state(units: list[Unit] | None = None, *, version: int = 1, **overrides: object) -> BuildState:
    base: dict[str, object] = {
        "plan": Plan(version=version, units=units if units is not None else []),
        "root": ROOT,
    }
    base.update(overrides)
    return BuildState(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 1. breaker pre-empt — highest precedence                                    #
# --------------------------------------------------------------------------- #
def test_breaker_trip_routes_to_review_above_all_else() -> None:
    # Even with a fresh eligible unit, the breaker wins.
    state = _state([_unit()], breaker_tripped=True)
    assert POLICY.decide(state) == RouteToReview(ReviewReason.BREAKER)


# --------------------------------------------------------------------------- #
# 2. no plan → planner                                                        #
# --------------------------------------------------------------------------- #
def test_no_plan_runs_planner() -> None:
    assert POLICY.decide(_state(version=0)) == RunPlanner()


def test_empty_unit_plan_runs_planner() -> None:
    assert POLICY.decide(_state([], version=1)) == RunPlanner()


# --------------------------------------------------------------------------- #
# 3. pending replan → Replan (counter-bounded) else Review                    #
# --------------------------------------------------------------------------- #
def test_pending_replan_under_cap_replans() -> None:
    state = _state([_unit()], pending_replan="too coarse", replan_count=1)
    assert POLICY.decide(state) == Replan("too coarse")


def test_pending_replan_at_cap_routes_to_review() -> None:
    state = _state([_unit()], pending_replan="again", replan_count=2)
    assert POLICY.decide(state) == RouteToReview(ReviewReason.REPLAN_EXHAUSTED)


# --------------------------------------------------------------------------- #
# 4. eligible-unit micro-cycle: work → verify → reflect                       #
# --------------------------------------------------------------------------- #
def test_fresh_eligible_unit_runs_worker() -> None:
    state = _state([_unit(phase=UnitPhase.WORK)])
    assert POLICY.decide(state) == RunWorker("a")


def test_gate_passed_unit_runs_verifier() -> None:
    # Worker + objective gate done → phase VERIFY → verify the unit's scope.
    state = _state([_unit(phase=UnitPhase.VERIFY, attempts=1)])
    assert POLICY.decide(state) == RunVerifier("a")


def test_verify_rejected_unit_reflects() -> None:
    rejected = _unit(
        phase=UnitPhase.REFLECT,
        attempts=1,
        last_verdict=Verdict(scope="a", met=False, reasons=("missing test",)),
    )
    assert POLICY.decide(_state([rejected])) == Reflect("a")


def test_first_pending_unit_is_the_one_worked() -> None:
    state = _state(
        [
            _unit("a", status=UnitStatus.VERIFIED),
            _unit("b", phase=UnitPhase.WORK),
        ]
    )
    assert POLICY.decide(state) == RunWorker("b")


# --------------------------------------------------------------------------- #
# 4b. bail-outs while working a unit                                          #
# --------------------------------------------------------------------------- #
def test_attempts_exhausted_routes_to_review() -> None:
    exhausted = _unit(attempts=3, phase=UnitPhase.WORK)  # K == 3
    assert POLICY.decide(_state([exhausted])) == RouteToReview(
        ReviewReason.BLOCKED_UNITS
    )


def test_no_progress_routes_to_review() -> None:
    stalled = _unit(attempts=2, phase=UnitPhase.WORK, diff_hashes=("h", "h"))
    assert POLICY.decide(_state([stalled])) == RouteToReview(ReviewReason.NO_PROGRESS)


def test_differing_diff_hashes_are_not_a_stall() -> None:
    progressing = _unit(attempts=2, phase=UnitPhase.WORK, diff_hashes=("h1", "h2"))
    assert POLICY.decide(_state([progressing])) == RunWorker("a")


def test_single_attempt_is_never_a_stall() -> None:
    first = _unit(attempts=1, phase=UnitPhase.WORK, diff_hashes=("h1",))
    assert POLICY.decide(_state([first])) == RunWorker("a")


# --------------------------------------------------------------------------- #
# 5. any blocked unit → Review (D10)                                          #
# --------------------------------------------------------------------------- #
def test_any_blocked_unit_routes_to_review() -> None:
    state = _state(
        [
            _unit("a", status=UnitStatus.VERIFIED),
            _unit("b", status=UnitStatus.BLOCKED),
        ]
    )
    assert POLICY.decide(state) == RouteToReview(ReviewReason.BLOCKED_UNITS)


# --------------------------------------------------------------------------- #
# 6. all verified → whole-diff verify → Integrate / Review                    #
# --------------------------------------------------------------------------- #
def test_all_verified_verifies_whole_diff() -> None:
    state = _state([_unit("a", status=UnitStatus.VERIFIED)])
    assert POLICY.decide(state) == RunVerifier(WHOLE_DIFF)


def test_whole_diff_pass_integrates() -> None:
    state = _state(
        [_unit("a", status=UnitStatus.VERIFIED)],
        whole_verdict=Verdict(scope=WHOLE_DIFF, met=True),
    )
    assert POLICY.decide(state) == Integrate()


def test_whole_diff_reject_routes_to_review() -> None:
    state = _state(
        [_unit("a", status=UnitStatus.VERIFIED)],
        whole_verdict=Verdict(scope=WHOLE_DIFF, met=False, reasons=("regressed",)),
    )
    assert POLICY.decide(state) == RouteToReview(ReviewReason.WHOLE_DIFF_REJECTED)


# --------------------------------------------------------------------------- #
# Determinism                                                                 #
# --------------------------------------------------------------------------- #
def test_decision_is_pure_and_repeatable() -> None:
    state = _state([_unit(phase=UnitPhase.VERIFY, attempts=1)])
    first = POLICY.decide(state)
    second = POLICY.decide(state)
    assert first == second == RunVerifier("a")
