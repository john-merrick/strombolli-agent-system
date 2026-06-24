"""Tests for the shared :class:`BuildResult` contract (engine Phase 0).

The refactor is additive: ``loop.LoopResult`` must satisfy the new structural
protocol so both engines can feed the same finalize path. These tests assert the
conformance and the four new accessors (``artifact_path``, ``blocked_items``,
``completed_count``, ``usage_spans``) read the worktree PRD / iterations exactly
as the old free functions did — no behavioural change.
"""

from __future__ import annotations

import json
from pathlib import Path

from stromboli.breaker import BreakerConfig, BreakerTrip, CircuitBreaker, TripReason
from stromboli.engine.result import BuildResult, UsageSpan
from stromboli.loop import Iteration, LoopResult, RalphLoop, StopReason
from stromboli.prd import PRD_RELATIVE_PATH


def _write_prd(tmp_path: Path, items: list[dict[str, object]]) -> Path:
    prd = tmp_path / PRD_RELATIVE_PATH
    prd.parent.mkdir(parents=True, exist_ok=True)
    prd.write_text(json.dumps({"meta": {}, "items": items}), encoding="utf-8")
    return prd


def _result(prd_path: Path, **overrides: object) -> LoopResult:
    base: dict[str, object] = {
        "reason": StopReason.NO_ELIGIBLE,
        "iterations": (),
        "prd_path": prd_path,
        "progress_path": prd_path.parent / "progress.txt",
        "trip": None,
    }
    base.update(overrides)
    return LoopResult(**base)  # type: ignore[arg-type]


def test_loop_result_conforms_to_build_result(tmp_path: Path) -> None:
    """LoopResult is a structural BuildResult (runtime-checkable protocol)."""
    prd = _write_prd(tmp_path, [{"id": "a", "passes": True}])
    result = _result(prd)
    assert isinstance(result, BuildResult)


def test_artifact_path_is_the_prd(tmp_path: Path) -> None:
    prd = _write_prd(tmp_path, [])
    assert _result(prd).artifact_path == prd


def test_completed_count_counts_passing_items(tmp_path: Path) -> None:
    prd = _write_prd(
        tmp_path,
        [
            {"id": "a", "passes": True},
            {"id": "b", "passes": False},
            {"id": "c", "passes": True},
        ],
    )
    assert _result(prd).completed_count() == 2


def test_completed_count_is_zero_when_prd_unreadable(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "prd.json"
    assert _result(missing).completed_count() == 0


def test_blocked_items_reports_blocked_with_reasons(tmp_path: Path) -> None:
    prd = _write_prd(
        tmp_path,
        [
            {"id": "a", "passes": True},
            {"id": "b", "blocked": True, "blockReason": "flaky dep"},
        ],
    )
    blocked = _result(prd).blocked_items()
    assert [(b.id, b.reason) for b in blocked] == [("b", "flaky dep")]


def test_usage_spans_one_per_iteration_with_tokens(tmp_path: Path) -> None:
    prd = _write_prd(tmp_path, [])
    iterations = (
        Iteration(
            index=1,
            stdout="",
            data={"total_cost_usd": 0.5, "usage": {"input_tokens": 10, "output_tokens": 3}},
            result_text="",
        ),
        Iteration(
            index=2,
            stdout="",
            data={"input_tokens": 7, "output_tokens": 1},
            result_text="",
        ),
        Iteration(index=3, stdout="", data=None, result_text=""),
    )
    spans = _result(prd, iterations=iterations).usage_spans()
    assert spans == [
        UsageSpan(index=1, cost_usd=0.5, input_tokens=10, output_tokens=3),
        UsageSpan(index=2, cost_usd=None, input_tokens=7, output_tokens=1),
        UsageSpan(index=3, cost_usd=None, input_tokens=0, output_tokens=0),
    ]


def test_tripped_result_exposes_trip(tmp_path: Path) -> None:
    prd = _write_prd(tmp_path, [])
    trip = BreakerTrip(
        reason=TripReason.COST, iterations=2, total_cost_usd=9.0, limit=5.0
    )
    result = _result(prd, reason=StopReason.BREAKER, trip=trip)
    assert result.tripped is True
    assert result.trip is trip


def test_real_loop_run_produces_a_conforming_result(tmp_path: Path) -> None:
    """An actual RalphLoop run yields a BuildResult-conforming value."""
    _write_prd(tmp_path, [{"id": "x"}])

    def cc(argv: object, cwd: object) -> str:
        return json.dumps({"type": "result", "result": "<promise>COMPLETE</promise>"})

    breaker = CircuitBreaker(BreakerConfig(max_iterations=5, max_cost_usd=1.0))
    result = RalphLoop(run=cc, breaker=breaker).run(tmp_path)
    assert isinstance(result, BuildResult)
    assert result.completed_count() == 0
