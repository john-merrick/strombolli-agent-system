"""The Prefect triage flow runs end-to-end in-process (stub phases, offline)."""

from __future__ import annotations

import pytest

pytest.importorskip("prefect")

from stromboli.graph import GraphDeps  # noqa: E402
from stromboli.orchestration.paused import PausedIndex  # noqa: E402
from stromboli.orchestration.phases import TriagePhases  # noqa: E402
from stromboli.orchestration.prefect_flow import triage_flow  # noqa: E402
from tests.nodes._fakes import RoutingGateway  # noqa: E402


def test_prefect_triage_flow_reaches_done() -> None:
    # Stub phases (no gateway/coder) → deterministic happy path, no network.
    phases = TriagePhases(GraphDeps())
    final = triage_flow("task-1", source="cli", phases=phases)
    assert final.status == "done"


def test_prefect_triage_flow_suspends_on_escalate(tmp_path: object) -> None:
    from pathlib import Path

    # Force the verifier to escalate → the flow suspends (Queued) instead of finalizing.
    gw = RoutingGateway(
        {"Spec": {"goal": "g", "ambiguous": False},
         "Verdict": {"decision": "escalate", "reason": "no good"}}
    )
    index = PausedIndex(Path(str(tmp_path)) / "paused.db")
    phases = TriagePhases(
        GraphDeps(
            gateway=gw, reasoning_model="m", prompt_model="p", verifier_model="g",
            paused_index=index,
        )
    )
    final = triage_flow("t-esc", source="cli", phases=phases)
    assert final.status == "queued"
    row = index.get("t-esc")
    assert row is not None and row.ref == 1


def test_prefect_triage_flow_unbuildable_parks_to_review() -> None:
    # A worktree-setup failure (e.g. no repo) must park to Review, not crash-loop.
    gw = RoutingGateway({"Spec": {"goal": "g", "ambiguous": False}})

    class _Boom:
        def run(self, *_a: object, **_k: object) -> object:
            raise AssertionError("unreached")

        def run_tests(self, *_a: object, **_k: object) -> object:
            raise AssertionError("unreached")

    def _no_worktree(_state: object) -> object:
        raise ValueError("task has no Project relation")

    phases = TriagePhases(
        GraphDeps(
            gateway=gw, reasoning_model="m", prompt_model="p", verifier_model="g",
            coder=_Boom(), sandbox=_Boom(), worktree_for=_no_worktree,  # type: ignore[arg-type]
        )
    )
    final = triage_flow("t-nobuild", source="cli", phases=phases)
    assert final.status == "escalated"
    assert "unbuildable" in final.reflections[-1]
