"""Tests for Langfuse observability (US-013).

Each task build is a trace; each Ralph iteration is a span. Cost and token usage
parsed from the CC output are recorded on the trace, and circuit-breaker trips
and failures are tagged. The tracer is an injectable seam: a :class:`NullTracer`
is the no-op default and the Langfuse-backed tracer is only constructed when
enabled, so this suite never imports or talks to Langfuse — it drives the
recording orchestrator with a capturing tracer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stromboli.breaker import BreakerConfig, CircuitBreaker
from stromboli.loop import RalphLoop, StopReason
from stromboli.observability import (
    BuildTracer,
    NullTracer,
    build_tracer,
    record_build_trace,
)
from stromboli.prd import PRD_RELATIVE_PATH


class RecordingTracer(BuildTracer):
    """Captures every tracer call for assertions."""

    def __init__(self) -> None:
        self.started: dict[str, Any] | None = None
        self.iterations: list[dict[str, Any]] = []
        self.totals: dict[str, Any] | None = None
        self.tags: list[str] = []
        self.finished_error: str | None = None
        self.finished = False

    def start(self, *, task_id: str, name: str) -> None:
        self.started = {"task_id": task_id, "name": name}

    def iteration(
        self,
        *,
        index: int,
        cost_usd: float | None,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self.iterations.append(
            {
                "index": index,
                "cost_usd": cost_usd,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )

    def record_totals(
        self, *, cost_usd: float, input_tokens: int, output_tokens: int
    ) -> None:
        self.totals = {
            "cost_usd": cost_usd,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    def tag(self, *tags: str) -> None:
        self.tags.extend(tags)

    def finish(self, *, error: str | None = None) -> None:
        self.finished = True
        self.finished_error = error


def _task(**overrides: Any) -> Any:
    from stromboli.notion import Task

    base: dict[str, Any] = {
        "page_id": "page-xyz",
        "name": "Trace me",
        "project_ids": ("p",),
        "status": "Working on",
        "spec": "s",
        "assigned_to": "Agent",
        "ready": True,
        "needs_review": False,
        "pr_url": None,
        "cost": None,
        "tokens": None,
    }
    base.update(overrides)
    return Task(**base)


def _cc_json(result: str, **extra: Any) -> str:
    return json.dumps({"type": "result", "result": result, **extra})


def _run_loop(tmp_path: Path, outputs: list[str], **loop_kwargs: Any) -> Any:
    prd = tmp_path / PRD_RELATIVE_PATH
    prd.parent.mkdir(parents=True, exist_ok=True)
    prd.write_text(json.dumps({"meta": {}, "items": [{"id": "a"}]}), encoding="utf-8")

    calls = {"n": 0}

    def cc(argv: Any, cwd: Any) -> str:
        i = calls["n"]
        calls["n"] += 1
        return outputs[min(i, len(outputs) - 1)]

    return RalphLoop(run=cc, **loop_kwargs).run(tmp_path)


# --------------------------------------------------------------------------- #
# Trace + spans + usage                                                       #
# --------------------------------------------------------------------------- #


def test_records_a_span_per_iteration_with_usage(tmp_path: Path) -> None:
    from stromboli.loop import COMPLETION_SIGNAL

    result = _run_loop(
        tmp_path,
        [
            _cc_json(
                "worked",
                total_cost_usd=0.10,
                usage={"input_tokens": 100, "output_tokens": 50},
            ),
            _cc_json(
                f"done {COMPLETION_SIGNAL}",
                total_cost_usd=0.20,
                usage={"input_tokens": 200, "output_tokens": 60},
            ),
        ],
        max_iterations=10,
    )
    assert result.reason is StopReason.COMPLETE

    tracer = RecordingTracer()
    record_build_trace(tracer, _task(), result)

    # One trace for the build, one span per iteration.
    assert tracer.started == {"task_id": "page-xyz", "name": "Trace me"}
    assert len(tracer.iterations) == 2
    assert tracer.iterations[0]["cost_usd"] == pytest.approx(0.10)
    assert tracer.iterations[1]["input_tokens"] == 200

    # Totals are summed across iterations and recorded on the trace.
    assert tracer.totals is not None
    assert tracer.totals["cost_usd"] == pytest.approx(0.30)
    assert tracer.totals["input_tokens"] == 300
    assert tracer.totals["output_tokens"] == 110
    assert tracer.finished is True
    assert tracer.finished_error is None


# --------------------------------------------------------------------------- #
# Trip + failure tagging                                                       #
# --------------------------------------------------------------------------- #


def test_breaker_trip_is_tagged_on_the_trace(tmp_path: Path) -> None:
    breaker = CircuitBreaker(BreakerConfig(max_iterations=100, max_cost_usd=1.0))
    result = _run_loop(
        tmp_path,
        [_cc_json("grind", total_cost_usd=0.8)],
        max_iterations=50,
        breaker=breaker,
    )
    assert result.reason is StopReason.BREAKER

    tracer = RecordingTracer()
    record_build_trace(tracer, _task(), result)

    joined = " ".join(tracer.tags)
    assert "circuit-breaker" in joined
    assert "cost_ceiling" in joined  # the specific trip reason is tagged


def test_failure_is_tagged_on_the_trace(tmp_path: Path) -> None:
    result = _run_loop(
        tmp_path, [_cc_json("grind")], max_iterations=2
    )  # ceiling, not complete

    tracer = RecordingTracer()
    record_build_trace(tracer, _task(), result, error="boom")

    assert "failure" in tracer.tags
    assert tracer.finished_error == "boom"


# --------------------------------------------------------------------------- #
# Null tracer / factory                                                       #
# --------------------------------------------------------------------------- #


def test_null_tracer_is_a_safe_no_op(tmp_path: Path) -> None:
    result = _run_loop(tmp_path, [_cc_json("grind")], max_iterations=1)
    # Must not raise with the default no-op tracer.
    record_build_trace(NullTracer(), _task(), result)


def test_build_tracer_returns_null_when_disabled() -> None:
    tracer = build_tracer(enabled=False)
    assert isinstance(tracer, NullTracer)
