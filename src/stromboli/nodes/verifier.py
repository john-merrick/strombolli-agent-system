"""Reflective Verifier node (PRD §6.5) — the outer recursion's judge.

A single structured call via the LiteLLM gateway on a **non-Claude** model
(Gemini 2.5 Pro, PRD §11.1) so its judgment is independent of the coder. It
checks the diff against the *spec intent* (not just green tests) and whether the
tests actually covered the acceptance criteria, returning a :class:`Verdict`.

On a non-pass it appends a reflection (persisted to episodic memory later, §7),
which on a ``revise`` is injected into the next coding pass (the outer loop,
bounded by ``MAX_OUTER_REVISIONS`` — see the verdict gate, §6.6).

With no gateway it falls back to a stub ``pass`` so the graph flows offline.
"""

from __future__ import annotations

import logging

from stromboli.llm.gateway import Gateway, GatewayError
from stromboli.nodes.intake import Node
from stromboli.state import StromboliState, TestResult, Verdict

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are an independent code reviewer for an autonomous coding system. You "
    "did NOT write this code. Judge the diff against the SPEC INTENT, not merely "
    "whether tests are green: decide whether the change actually satisfies every "
    "acceptance criterion, and whether the tests genuinely cover them (flag "
    "hollow or tautological tests). Decide pass (ships), revise (fixable — say "
    "exactly what to fix), or escalate (needs a human). Be specific in 'reason'."
)


def _render_tests(results: list[TestResult]) -> str:
    if not results:
        return "(no test runs recorded)"
    last = results[-1]
    status = "PASSED" if last.passed else "FAILED"
    return f"Latest sandbox test run: {status}\n{last.summary}\n{last.raw}"


def make_verifier(gateway: Gateway | None = None, *, model: str | None = None) -> Node:
    """Build the verifier node. With no ``gateway`` it returns a stub pass."""

    def verifier(state: StromboliState) -> dict[str, object]:
        if gateway is None or model is None:
            verdict = Verdict(
                decision="pass",
                reason="stub: accepted",
                coverage_note="stub: tests cover the spec",
            )
            return {"verdict": verdict, "status": "verifying"}

        spec = state.spec
        criteria = "\n".join(f"- {c}" for c in spec.acceptance_criteria) if spec else ""
        user = (
            f"# Goal\n{spec.goal if spec else state.raw_request}\n\n"
            f"# Acceptance criteria\n{criteria or '(none specified)'}\n\n"
            f"# Diff under review\n{state.code_diff or '(empty diff)'}\n\n"
            f"# Test evidence\n{_render_tests(state.test_results)}"
        )
        try:
            verdict = gateway.structured(
                model=model, system=_SYSTEM, user=user, schema=Verdict
            )
        except GatewayError:
            logger.exception("Verifier gateway call failed; escalating.")
            verdict = Verdict(
                decision="escalate",
                reason="verifier model call failed — needs human review",
            )

        update: dict[str, object] = {"verdict": verdict, "status": "verifying"}
        if verdict.decision != "pass":
            update["reflections"] = [f"{verdict.decision}: {verdict.reason}"]
        return update

    return verifier


__all__ = ["make_verifier"]
