"""The multi-agent build engine — a graph-based alternative to the Ralph loop.

This subpackage houses the flag-gated ``graph`` engine (planner → worker →
verifier → reflector, driven by a deterministic control policy) alongside the
shared :class:`~stromboli.engine.result.BuildResult` contract that lets either
engine feed the same finalize / write-back / observability path.

The engine is additive: Ralph (:mod:`stromboli.loop`) remains the default and
fully functional until ``STROMBOLI_ENGINE=graph`` is deliberately set.
"""

from __future__ import annotations

from stromboli.engine.result import BuildResult, UsageSpan

__all__ = [
    "BuildResult",
    "UsageSpan",
]
