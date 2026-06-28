"""End-to-end graph tests for the orchestrator (Phase 4).

The whole engine is driven with fake agents (a scripted CC runner), a fake gate,
and a deterministic diff provider — no git, no network, no real ``claude``. Each
test scripts a path through the graph and asserts the terminal BuildResult:
happy path, reflection, replan, breaker trip, K-exhaustion → Review, resume from
disk, and a forward-compat check that a different policy drives the same nodes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

from stromboli.breaker import BreakerConfig
from stromboli.engine.actions import Action
from stromboli.engine.gate import GateResult
from stromboli.engine.orchestrator import (
    INTEGRATED,
    GraphEngine,
    GraphResult,
)
from stromboli.engine.policy import ControlPolicy, DeterministicPolicy
from stromboli.engine.result import BuildResult
from stromboli.engine.state import (
    BuildState,
    Plan,
    Unit,
    UnitStatus,
)

SPEC = "Build a /health endpoint."


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
class Agents:
    """A scripted CC runner: returns queued agent answers, each with a cost."""

    def __init__(self, *answers: str, cost: float = 0.0) -> None:
        self._answers = list(answers)
        self.cost = cost
        self.calls = 0
        self.prompts: list[str] = []

    def __call__(self, argv: Sequence[str], cwd: Path) -> str:
        self.prompts.append(argv[2])
        i = min(self.calls, len(self._answers) - 1)
        self.calls += 1
        return json.dumps(
            {"type": "result", "result": self._answers[i], "total_cost_usd": self.cost}
        )


class FakeGate:
    """Returns queued :class:`GateResult`\\ s per ``run`` call (last repeats)."""

    def __init__(self, *results: GateResult) -> None:
        self._results = list(results) or [GateResult(passed=True)]
        self.calls = 0

    def run(self, cwd: str | Path) -> GateResult:
        i = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return self._results[i]


def _incrementing_diff() -> Callable[[Path], str]:
    state = {"n": 0}

    def diff(root: Path) -> str:
        state["n"] += 1
        return f"diff-{state['n']}"

    return diff


def _plan(*criteria: str) -> str:
    return json.dumps(
        {"units": [{"description": c, "acceptance_criterion": c} for c in criteria]}
    )


def _verdict(met: bool, *, hollow: bool = False, reasons: list[str] | None = None) -> str:
    return json.dumps({"met": met, "hollow_tests": hollow, "reasons": reasons or []})


def _reflect(*, feedback: str = "fix it", replan: bool = False, reason: str = "") -> str:
    return json.dumps(
        {"feedback": feedback, "replan": replan, "replan_reason": reason}
    )


WORKED = "edited files"


def _engine(
    agents: Agents,
    *,
    policy: ControlPolicy | None = None,
    gate: FakeGate | None = None,
    diff: Callable[[Path], str] | None = None,
    breaker: BreakerConfig | None = None,
    max_attempts: int = 3,
) -> GraphEngine:
    return GraphEngine(
        policy=policy or DeterministicPolicy(max_attempts=max_attempts, max_replans=2),
        gate=gate or FakeGate(GateResult(passed=True)),
        cc_run=agents,
        breaker_config=breaker or BreakerConfig(max_iterations=50, max_cost_usd=1000.0),
        diff=diff or _incrementing_diff(),
    )


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #
def test_happy_path_integrates(tmp_path: Path) -> None:
    agents = Agents(
        _plan("GET /health returns 200"),  # planner
        WORKED,  # worker
        _verdict(True),  # unit verifier
        _verdict(True),  # whole-diff verifier
    )
    result = _engine(agents).run(tmp_path, spec=SPEC)
    assert isinstance(result, BuildResult)
    assert result.reason == INTEGRATED
    assert result.tripped is False
    assert result.completed_count() == 1
    assert result.blocked_items() == []
    assert agents.calls == 4


def test_state_is_persisted(tmp_path: Path) -> None:
    agents = Agents(_plan("c"), WORKED, _verdict(True), _verdict(True))
    result = _engine(agents).run(tmp_path, spec=SPEC)
    assert result.artifact_path.exists()
    reloaded = BuildState.load(tmp_path)
    assert reloaded.all_verified()


# --------------------------------------------------------------------------- #
# Reflection path                                                             #
# --------------------------------------------------------------------------- #
def test_reflection_then_success(tmp_path: Path) -> None:
    agents = Agents(
        _plan("c"),  # planner
        WORKED,  # worker attempt 1
        _verdict(False, reasons=["hollow test"]),  # verifier rejects
        _reflect(feedback="assert the real value"),  # reflector → feedback
        WORKED,  # worker attempt 2
        _verdict(True),  # verifier accepts
        _verdict(True),  # whole-diff
    )
    result = _engine(agents).run(tmp_path, spec=SPEC)
    assert result.reason == INTEGRATED
    assert result.completed_count() == 1
    # The reflector feedback reached the second worker prompt.
    assert any("assert the real value" in p for p in agents.prompts)


def test_hollow_tests_block_a_nominal_pass(tmp_path: Path) -> None:
    # Verifier reports met=true BUT hollow_tests=true → not verified, reflect.
    agents = Agents(
        _plan("c"),
        WORKED,
        _verdict(True, hollow=True),  # nominal pass, but hollow → reject
        _reflect(feedback="write a real test"),
        WORKED,
        _verdict(True),
        _verdict(True),
    )
    result = _engine(agents).run(tmp_path, spec=SPEC)
    assert result.reason == INTEGRATED


# --------------------------------------------------------------------------- #
# Replan path                                                                 #
# --------------------------------------------------------------------------- #
def test_replan_then_success(tmp_path: Path) -> None:
    agents = Agents(
        _plan("too coarse"),  # planner v1
        WORKED,  # worker
        _verdict(False, reasons=["unit conflates concerns"]),  # reject
        _reflect(replan=True, reason="split into two units"),  # → replan
        _plan("part A"),  # planner v2
        WORKED,  # worker
        _verdict(True),  # unit verifier
        _verdict(True),  # whole-diff
    )
    result = _engine(agents).run(tmp_path, spec=SPEC)
    assert result.reason == INTEGRATED
    state = BuildState.load(tmp_path)
    assert state.replan_count == 1
    assert state.plan.version == 2


# --------------------------------------------------------------------------- #
# Breaker trip                                                                #
# --------------------------------------------------------------------------- #
def test_breaker_trip_routes_to_review(tmp_path: Path) -> None:
    agents = Agents(_plan("c"), WORKED, cost=5.0)
    engine = _engine(
        agents, breaker=BreakerConfig(max_iterations=50, max_cost_usd=1.0)
    )
    result = engine.run(tmp_path, spec=SPEC)
    assert result.reason == "breaker"
    assert result.tripped is True
    assert result.trip is not None
    # The planner alone (cost 5.0 > 1.0 ceiling) tripped it.
    assert agents.calls == 1


# --------------------------------------------------------------------------- #
# K-exhaustion → Review (D10)                                                 #
# --------------------------------------------------------------------------- #
def test_attempts_exhausted_routes_to_review(tmp_path: Path) -> None:
    agents = Agents(
        _plan("c"),
        WORKED,  # attempt 1
        _verdict(False, reasons=["still wrong"]),
        _reflect(feedback="try harder"),
        WORKED,  # attempt 2 (== K)
    )
    result = _engine(agents, max_attempts=2).run(tmp_path, spec=SPEC)
    assert result.reason == "blocked_units"
    assert result.completed_count() == 0
    blocked = result.blocked_items()
    assert len(blocked) == 1
    assert blocked[0].reason  # carries the last verdict reason / a fallback


def test_no_progress_routes_to_review(tmp_path: Path) -> None:
    # A constant diff → identical hashes across attempts → stall.
    agents = Agents(
        _plan("c"),
        WORKED,
        _verdict(False, reasons=["nope"]),
        _reflect(feedback="again"),
        WORKED,
    )
    result = _engine(
        agents, diff=lambda root: "unchanging", max_attempts=5
    ).run(tmp_path, spec=SPEC)
    assert result.reason == "no_progress"
    assert result.blocked_items()


# --------------------------------------------------------------------------- #
# Gate failure feeds back to the worker                                       #
# --------------------------------------------------------------------------- #
def test_gate_failure_keeps_unit_in_work(tmp_path: Path) -> None:
    # Gate fails the first worker pass, passes the second.
    gate = FakeGate(
        GateResult(passed=False, output="1 failed", command=("pytest",)),
        GateResult(passed=True),
    )
    agents = Agents(
        _plan("c"),
        WORKED,  # attempt 1 → gate fails → stays WORK
        WORKED,  # attempt 2 → gate passes → VERIFY
        _verdict(True),
        _verdict(True),
    )
    result = _engine(agents, gate=gate).run(tmp_path, spec=SPEC)
    assert result.reason == INTEGRATED
    # The failing test output was surfaced to the worker as feedback.
    assert any("1 failed" in p for p in agents.prompts)


# --------------------------------------------------------------------------- #
# Resume from disk                                                            #
# --------------------------------------------------------------------------- #
def test_resume_from_disk_continues(tmp_path: Path) -> None:
    # Seed a state where the only unit is already verified; resume should just
    # run the whole-diff verification and integrate — no re-plan, no re-work.
    seeded = BuildState(
        plan=Plan(
            version=1,
            units=[
                Unit(
                    id="AC-001",
                    description="d",
                    acceptance_criterion="c",
                    status=UnitStatus.VERIFIED,
                )
            ],
        ),
        root=tmp_path,
    )
    seeded.save()

    agents = Agents(_verdict(True))  # only the whole-diff verifier should run
    result = _engine(agents).run(tmp_path, spec=SPEC, resume=True)
    assert result.reason == INTEGRATED
    assert agents.calls == 1


# --------------------------------------------------------------------------- #
# Forward-compat: a different policy drives the same nodes (D7)               #
# --------------------------------------------------------------------------- #
class SupervisorPolicy:
    """A stand-in 'smarter' policy: here it simply delegates, proving the
    engine drives its nodes through the ControlPolicy seam, not a concrete type."""

    def __init__(self) -> None:
        self._inner = DeterministicPolicy()
        self.decisions: list[str] = []

    def decide(self, state: BuildState) -> Action:
        action = self._inner.decide(state)
        self.decisions.append(type(action).__name__)
        return action


def test_alternate_policy_drives_the_same_nodes(tmp_path: Path) -> None:
    policy = SupervisorPolicy()
    agents = Agents(_plan("c"), WORKED, _verdict(True), _verdict(True))
    result = _engine(agents, policy=policy).run(tmp_path, spec=SPEC)
    assert result.reason == INTEGRATED
    # The supervisor actually made every decision.
    assert "RunPlanner" in policy.decisions
    assert "Integrate" in policy.decisions


def test_graph_result_conforms_to_build_result(tmp_path: Path) -> None:
    agents = Agents(_plan("c"), WORKED, _verdict(True), _verdict(True))
    result = _engine(agents).run(tmp_path, spec=SPEC)
    assert isinstance(result, GraphResult)
    assert isinstance(result, BuildResult)


def test_engine_reports_a_stage_per_step(tmp_path: Path) -> None:
    from stromboli import reporting

    agents = Agents(_plan("c"), WORKED, _verdict(True), _verdict(True))
    stages: list[str] = []
    with reporting.using(stages.append):
        _engine(agents).run(tmp_path, spec=SPEC)
    # One stage per graph step, each naming the action and its outcome.
    assert any(s.startswith("RunPlanner") for s in stages)
    assert any(s.startswith("RunVerifier") for s in stages)
    assert stages[-1].startswith("Integrate")


# --------------------------------------------------------------------------- #
# Forensics: transcripts, enriched history, feedback trail                    #
# --------------------------------------------------------------------------- #
def test_each_step_writes_a_transcript(tmp_path: Path) -> None:
    agents = Agents(_plan("c"), WORKED, _verdict(True), _verdict(True))
    _engine(agents).run(tmp_path, spec=SPEC)

    transcripts = sorted((tmp_path / ".stromboli" / "transcripts").glob("*.md"))
    names = [p.name for p in transcripts]
    # One file per step, ordered, named by action.
    assert names == [
        "001-RunPlanner.md",
        "002-RunWorker.md",
        "003-RunVerifier.md",
        "004-RunVerifier.md",
        "005-Integrate.md",
    ]


def test_transcript_captures_prompt_and_raw_output(tmp_path: Path) -> None:
    agents = Agents(_plan("GET /health"), WORKED, _verdict(True), _verdict(True))
    _engine(agents).run(tmp_path, spec=SPEC)

    planner_md = (
        tmp_path / ".stromboli" / "transcripts" / "001-RunPlanner.md"
    ).read_text(encoding="utf-8")
    # The exact prompt the planner saw and its raw output are both recorded.
    assert "Decompose the task" in planner_md
    assert "GET /health" in planner_md
    assert "Exchange 1 — raw output" in planner_md


def test_worker_transcript_records_the_gate(tmp_path: Path) -> None:
    gate = FakeGate(
        GateResult(passed=False, output="1 failed", command=("pytest",)),
        GateResult(passed=True),
    )
    agents = Agents(_plan("c"), WORKED, WORKED, _verdict(True), _verdict(True))
    _engine(agents, gate=gate).run(tmp_path, spec=SPEC)

    worker_md = (
        tmp_path / ".stromboli" / "transcripts" / "002-RunWorker.md"
    ).read_text(encoding="utf-8")
    assert "## Objective gate" in worker_md
    assert "pytest → failed" in worker_md
    assert "1 failed" in worker_md


def test_history_is_enriched_with_outcomes(tmp_path: Path) -> None:
    agents = Agents(
        _plan("c"),
        WORKED,
        _verdict(False, reasons=["hollow"]),
        _reflect(feedback="add a real assertion"),
        WORKED,
        _verdict(True),
        _verdict(True),
    )
    _engine(agents).run(tmp_path, spec=SPEC)

    state = BuildState.load(tmp_path)
    # Every history entry has a step, action, outcome, and transcript pointer.
    assert all(
        {"step", "action", "outcome", "transcript"} <= set(entry)
        for entry in state.history
    )
    verifier_entries = [e for e in state.history if e["action"] == "RunVerifier"]
    # The first verifier rejected with its reason captured on the timeline.
    assert verifier_entries[0]["met"] is False
    assert verifier_entries[0]["reasons"] == ["hollow"]


def test_feedback_trail_accumulates_across_attempts(tmp_path: Path) -> None:
    agents = Agents(
        _plan("c"),
        WORKED,
        _verdict(False, reasons=["r1"]),
        _reflect(feedback="first round feedback"),
        WORKED,
        _verdict(False, reasons=["r2"]),
        _reflect(feedback="second round feedback"),
        WORKED,
        _verdict(True),
        _verdict(True),
    )
    _engine(agents).run(tmp_path, spec=SPEC)

    feedback_md = (tmp_path / ".stromboli" / "feedback.md").read_text(encoding="utf-8")
    # Both reflections survive on disk (the trail was not overwritten).
    assert "first round feedback" in feedback_md
    assert "second round feedback" in feedback_md
