"""Reflective Verifier node (PRD §6.5) — the outer recursion's judge.

Real behavior (Phase 3): a single structured call via the LiteLLM gateway on a
**non-Claude** model (Gemini 2.5 Pro) that checks the diff against the *spec
intent* (not just green tests) and whether the tests covered the acceptance
criteria, returning a :class:`Verdict`. On a non-pass it appends a reflection.

Phase 0 stub: always pass, so the graph flows to PR end-to-end.
"""

from __future__ import annotations

from stromboli.nodes.intake import Node
from stromboli.state import StromboliState, Verdict


def make_verifier() -> Node:
    """Build the verifier node (Phase 0 stub; Gemini judge lands in Phase 3)."""

    def verifier(state: StromboliState) -> dict[str, object]:
        verdict = Verdict(
            decision="pass",
            reason="stub: accepted",
            coverage_note="stub: tests cover the spec",
        )
        return {"verdict": verdict, "status": "verifying"}

    return verifier


__all__ = ["make_verifier"]
