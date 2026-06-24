"""The GraphEngine: drive policy → node → persist until terminal (Phase 4).

This is the engine's loop. Each turn it asks the (pure) policy for the next
:class:`~stromboli.engine.actions.Action`, dispatches the matching agent node or
objective check, applies the outcome onto the :class:`BuildState`, persists the
state (so a crash is resumable), and meters the agent's cost into the circuit
breaker. When the breaker ceiling is crossed it flips ``breaker_tripped`` so the
*policy* — not the loop — pre-empts the build to Review on the next turn,
keeping control flow in one place.

The loop terminates on exactly two actions: :class:`Integrate` (success) and
:class:`RouteToReview`. It returns a :class:`GraphResult`, which conforms to
:class:`~stromboli.engine.result.BuildResult` so the shared integrator /
finalize path (Phase 3) handles it identically to a Ralph run.

Agents are reached only through the injected :data:`~stromboli.loop.CCRunner`
(wrapped in a cost meter), and the worktree diff through an injected ``diff``
callable — so the whole graph is deterministic and unit-tested with fakes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from stromboli.breaker import BreakerConfig, BreakerTrip, CircuitBreaker
from stromboli.engine.actions import (
    Action,
    Integrate,
    Reflect,
    Replan,
    ReviewReason,
    RunPlanner,
    RunVerifier,
    RunWorker,
)
from stromboli.engine.gate import Gate
from stromboli.engine.nodes.planner import PlannerNode
from stromboli.engine.nodes.reflector import ReflectorNode, ReflectTarget
from stromboli.engine.nodes.verifier import VerifierNode, VerifyTarget
from stromboli.engine.nodes.worker import WorkerNode
from stromboli.engine.policy import ControlPolicy
from stromboli.engine.result import UsageSpan
from stromboli.engine.state import (
    STATE_DIR,
    STATE_FILE,
    WHOLE_DIFF,
    BuildState,
    Unit,
    UnitPhase,
    UnitStatus,
)
from stromboli.loop import CCRunner
from stromboli.writeback import BlockedItem

logger = logging.getLogger(__name__)

#: Hard ceiling on loop turns — a backstop above the breaker's iteration cap.
DEFAULT_MAX_STEPS = 200
#: The terminal reason recorded for a successful integration.
INTEGRATED = "integrated"


class EngineNodes(NamedTuple):
    """The four agent nodes, all sharing one (metered) CC runner."""

    planner: PlannerNode
    worker: WorkerNode
    verifier: VerifierNode
    reflector: ReflectorNode


def _parse_cc_usage(stdout: str) -> tuple[float | None, int, int]:
    """Extract ``(cost_usd, input_tokens, output_tokens)`` from a CC payload."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return (None, 0, 0)
    if not isinstance(data, dict):
        return (None, 0, 0)
    raw_cost = data.get("total_cost_usd")
    cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
    usage = data.get("usage")
    source: dict[str, Any] = usage if isinstance(usage, dict) else data

    def _int(key: str) -> int:
        value = source.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    return (cost, _int("input_tokens"), _int("output_tokens"))


class _MeteredRunner:
    """Wraps a :data:`CCRunner`, recording a :class:`UsageSpan` per CC call and
    accumulating cost between :meth:`pop_cost` reads (one read per agent step)."""

    def __init__(self, run: CCRunner) -> None:
        self._run = run
        self.spans: list[UsageSpan] = []
        self._pending_cost = 0.0

    def __call__(self, argv: Sequence[str], cwd: Path) -> str:
        stdout = self._run(argv, cwd)
        cost, input_tokens, output_tokens = _parse_cc_usage(stdout)
        self.spans.append(
            UsageSpan(
                index=len(self.spans) + 1,
                cost_usd=cost,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
        self._pending_cost += cost or 0.0
        return stdout

    def pop_cost(self) -> float:
        """The cost accrued since the last read (a whole agent step)."""
        cost = self._pending_cost
        self._pending_cost = 0.0
        return cost


def _git_diff(root: Path) -> str:
    """Default diff provider: the worktree's tracked changes against HEAD."""
    proc = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "diff", "HEAD"], capture_output=True, text=True
    )
    return proc.stdout


@dataclass(frozen=True)
class GraphResult:
    """The engine's outcome — a :class:`BuildResult` for the shared finalize."""

    reason: str
    root: Path
    units: tuple[Unit, ...]
    trip: BreakerTrip | None
    spans: tuple[UsageSpan, ...]

    @property
    def tripped(self) -> bool:
        return self.trip is not None

    @property
    def artifact_path(self) -> Path:
        return self.root / STATE_DIR / STATE_FILE

    def blocked_items(self) -> list[BlockedItem]:
        return [
            BlockedItem(id=u.id, reason=u.block_reason)
            for u in self.units
            if u.status is UnitStatus.BLOCKED
        ]

    def completed_count(self) -> int:
        return sum(1 for u in self.units if u.status is UnitStatus.VERIFIED)

    def usage_spans(self) -> list[UsageSpan]:
        return list(self.spans)


class GraphEngine:
    """Runs the policy/node loop over a worktree to a terminal BuildResult."""

    def __init__(
        self,
        *,
        policy: ControlPolicy,
        gate: Gate,
        cc_run: CCRunner,
        breaker_config: BreakerConfig,
        diff: Callable[[Path], str] = _git_diff,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self._policy = policy
        self._gate = gate
        self._cc_run = cc_run
        self._breaker_config = breaker_config
        self._diff = diff
        self._max_steps = max_steps

    def run(
        self, worktree_root: str | Path, *, spec: str, resume: bool = False
    ) -> GraphResult:
        """Drive the graph in ``worktree_root`` to a terminal outcome."""
        root = Path(worktree_root)
        state = (
            BuildState.load(root)
            if resume and (root / STATE_DIR / STATE_FILE).exists()
            else BuildState.initial(root)
        )
        meter = _MeteredRunner(self._cc_run)
        nodes = self._build_nodes(meter)
        breaker = CircuitBreaker(self._breaker_config)
        trip_holder: list[BreakerTrip] = []

        terminal: str | None = None
        for _ in range(self._max_steps):
            action = self._policy.decide(state)
            terminal = self._dispatch(
                action, state, root, spec, nodes, breaker, meter, trip_holder
            )
            state.save()
            if terminal is not None:
                break
        else:
            # Backstop: the policy never terminated within the step ceiling.
            logger.warning("GraphEngine hit the %d-step ceiling.", self._max_steps)
            self._block_unverified(state, "engine step ceiling reached")
            state.save()
            terminal = ReviewReason.NO_PROGRESS.value

        return GraphResult(
            reason=terminal,
            root=root,
            units=tuple(state.plan.units),
            trip=trip_holder[0] if trip_holder else None,
            spans=tuple(meter.spans),
        )

    # -- dispatch --------------------------------------------------------- #
    def _dispatch(
        self,
        action: Action,
        state: BuildState,
        root: Path,
        spec: str,
        nodes: EngineNodes,
        breaker: CircuitBreaker,
        meter: _MeteredRunner,
        trip_holder: list[BreakerTrip],
    ) -> str | None:
        """Apply ``action``; return a terminal reason or ``None`` to continue."""
        state.record({"action": type(action).__name__})

        if isinstance(action, RunPlanner):
            state.plan = nodes.planner.plan(state, spec, root)
            self._meter_breaker(state, breaker, meter, trip_holder)
            return None

        if isinstance(action, Replan):
            state.replan_count += 1
            state.plan = nodes.planner.plan(state, spec, root)
            state.pending_replan = None
            self._meter_breaker(state, breaker, meter, trip_holder)
            return None

        if isinstance(action, RunWorker):
            self._run_worker(action.unit_id, state, root, nodes)
            self._meter_breaker(state, breaker, meter, trip_holder)
            return None

        if isinstance(action, RunVerifier):
            self._run_verifier(action.scope, state, root, nodes)
            self._meter_breaker(state, breaker, meter, trip_holder)
            return None

        if isinstance(action, Reflect):
            self._run_reflector(action.unit_id, state, root, nodes)
            self._meter_breaker(state, breaker, meter, trip_holder)
            return None

        if isinstance(action, Integrate):
            return INTEGRATED

        # RouteToReview — terminal; park any unfinished units.
        self._block_unverified(state, f"build routed to review ({action.reason.value})")
        return action.reason.value

    # -- node steps ------------------------------------------------------- #
    def _run_worker(
        self, unit_id: str, state: BuildState, root: Path, nodes: EngineNodes
    ) -> None:
        unit = state.unit(unit_id)
        if unit is None:  # pragma: no cover - policy never names a missing unit
            return
        nodes.worker.work(state, unit, root)
        unit.attempts += 1
        digest = hashlib.sha1(self._diff(root).encode("utf-8")).hexdigest()  # noqa: S324
        unit.diff_hashes = (*unit.diff_hashes, digest)

        gate_result = self._gate.run(root)
        if gate_result.passed:
            unit.phase = UnitPhase.VERIFY
        else:
            # Failed (or crashed) checks → another worker pass; surface the output
            # so the next attempt can react to the real test/lint errors.
            kind = "crashed" if gate_result.crashed else "failed"
            state.feedback = f"The objective checks {kind}:\n{gate_result.output}"
            unit.phase = UnitPhase.WORK

    def _run_verifier(
        self, scope: str, state: BuildState, root: Path, nodes: EngineNodes
    ) -> None:
        diff = self._diff(root)
        if scope == WHOLE_DIFF:
            criterion = "\n".join(u.acceptance_criterion for u in state.plan.units)
            target = VerifyTarget(scope=WHOLE_DIFF, acceptance_criterion=criterion, diff=diff)
            state.whole_verdict = nodes.verifier.verify(state, target, root)
            return

        unit = state.unit(scope)
        if unit is None:  # pragma: no cover - policy never names a missing unit
            return
        target = VerifyTarget(
            scope=unit.id,
            acceptance_criterion=unit.acceptance_criterion,
            diff=diff,
            definition_of_done=unit.definition_of_done,
        )
        verdict = nodes.verifier.verify(state, target, root)
        unit.last_verdict = verdict
        # Hollow tests fail the unit even if the criterion is nominally "met".
        if verdict.met and not verdict.hollow_tests:
            unit.status = UnitStatus.VERIFIED
        else:
            unit.phase = UnitPhase.REFLECT

    def _run_reflector(
        self, unit_id: str, state: BuildState, root: Path, nodes: EngineNodes
    ) -> None:
        unit = state.unit(unit_id)
        if unit is None:  # pragma: no cover - policy never names a missing unit
            return
        reasons = unit.last_verdict.reasons if unit.last_verdict else ()
        target = ReflectTarget(
            unit_id=unit.id,
            acceptance_criterion=unit.acceptance_criterion,
            reasons=reasons,
            diff=self._diff(root),
        )
        reflection = nodes.reflector.reflect(state, target, root)
        if reflection.wants_replan:
            state.pending_replan = (
                reflection.replan_reason or "reflector requested a replan"
            )
        else:
            state.feedback = reflection.feedback
            unit.phase = UnitPhase.WORK

    # -- helpers ---------------------------------------------------------- #
    def _meter_breaker(
        self,
        state: BuildState,
        breaker: CircuitBreaker,
        meter: _MeteredRunner,
        trip_holder: list[BreakerTrip],
    ) -> None:
        """Record the just-run agent step's cost; flip the trip flag on ceiling."""
        trip = breaker.record_iteration(meter.pop_cost())
        if trip is not None and not trip_holder:
            trip_holder.append(trip)
            state.breaker_tripped = True

    @staticmethod
    def _block_unverified(state: BuildState, reason: str) -> None:
        """Park every not-yet-verified unit as BLOCKED (for the Review hand-off)."""
        for unit in state.plan.units:
            if unit.status is not UnitStatus.VERIFIED:
                unit.status = UnitStatus.BLOCKED
                if not unit.block_reason:
                    last = unit.last_verdict
                    unit.block_reason = (
                        "; ".join(last.reasons) if last and last.reasons else reason
                    )

    def _build_nodes(self, meter: _MeteredRunner) -> EngineNodes:
        return EngineNodes(
            planner=PlannerNode(meter),
            worker=WorkerNode(meter),
            verifier=VerifierNode(meter),
            reflector=ReflectorNode(meter),
        )


__all__ = [
    "DEFAULT_MAX_STEPS",
    "INTEGRATED",
    "EngineNodes",
    "GraphEngine",
    "GraphResult",
]
