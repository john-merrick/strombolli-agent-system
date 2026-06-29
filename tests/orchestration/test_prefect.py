"""The Prefect triage flow runs end-to-end in-process (stub phases, offline)."""

from __future__ import annotations

import pytest

pytest.importorskip("prefect")

from stromboli.graph import GraphDeps  # noqa: E402
from stromboli.orchestration.phases import TriagePhases  # noqa: E402
from stromboli.orchestration.prefect_flow import triage_flow  # noqa: E402


def test_prefect_triage_flow_reaches_done() -> None:
    # Stub phases (no gateway/coder) → deterministic happy path, no network.
    phases = TriagePhases(GraphDeps())
    final = triage_flow("task-1", source="cli", phases=phases)
    assert final.status == "done"
