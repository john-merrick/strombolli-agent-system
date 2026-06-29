"""Coding node (PRD §6.4) — the inner recursive write→test→fix loop.

Real behavior (Phase 2): call the Claude Agent SDK with the spec as the prompt
and the sandboxed worktree as cwd; the SDK's agent loop *is* the recursion. We
only bound it (``MAX_INNER_TURNS`` + token ceiling), constrain it (tool allowlist
+ fail-closed perms), and capture it (diff, last test output, message stream,
``session_id``). The only oracle is the sandbox test run.

Phase 0 stub: emit a fake diff + a passing test result so the graph flows to the
verifier without launching the SDK or Docker.
"""

from __future__ import annotations

from stromboli.nodes.intake import Node
from stromboli.state import StromboliState, TestResult


def make_coding() -> Node:
    """Build the coding node (Phase 0 stub; Agent SDK wrapper lands in Phase 2)."""

    def coding(state: StromboliState) -> dict[str, object]:
        return {
            "code_diff": "diff --git a/stub b/stub\n+stub",
            "test_results": [TestResult(passed=True, summary="stub: tests green")],
            "inner_iterations": 1,
            "session_id": "stub-session",
            "status": "coding",
        }

    return coding


__all__ = ["make_coding"]
