"""The multi-agent build engine — a graph-based alternative to the Ralph loop.

This subpackage houses the flag-gated ``graph`` engine (planner → worker →
verifier → reflector, driven by a deterministic control policy) alongside the
shared :class:`~stromboli.engine.result.BuildResult` contract that lets either
engine feed the same finalize / write-back / observability path.

The engine is additive: Ralph (:mod:`stromboli.loop`) remains the default and
fully functional until ``STROMBOLI_ENGINE=graph`` is deliberately set.
"""

from __future__ import annotations

from stromboli.engine.gate import GateResult, ObjectiveGate
from stromboli.engine.integrate import finalize_build, integrate_build
from stromboli.engine.orchestrator import GraphEngine, GraphResult
from stromboli.engine.policy import ControlPolicy, DeterministicPolicy
from stromboli.engine.result import BuildResult, UsageSpan

__all__ = [
    "BuildResult",
    "ControlPolicy",
    "DeterministicPolicy",
    "GateResult",
    "GraphEngine",
    "GraphResult",
    "ObjectiveGate",
    "UsageSpan",
    "finalize_build",
    "integrate_build",
]
