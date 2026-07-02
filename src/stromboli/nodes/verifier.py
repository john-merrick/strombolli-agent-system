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
from collections.abc import Callable
from typing import Any

from stromboli.llm.gateway import Gateway, GatewayError, usage_tokens
from stromboli.nodes.intake import Node
from stromboli.state import StromboliState, TestResult, Verdict

logger = logging.getLogger(__name__)

#: The verifier's system prompt — the judge, the highest-leverage prompt. Kept
#: as a module constant *and* an injectable parameter of :func:`make_verifier`
#: so the GEPA/DSPy optimizer (self-improving §2) can propose a replacement,
#: validate it against the labelled dataset, and adopt it without touching the
#: LangGraph orchestration.
DEFAULT_VERIFIER_SYSTEM = (
    "You are an independent code reviewer for an autonomous coding system. You "
    "did NOT write this code. Judge the diff against the SPEC INTENT, not merely "
    "whether tests are green: decide whether the change actually satisfies every "
    "acceptance criterion, and whether the tests genuinely cover them (flag "
    "hollow or tautological tests). Decide pass (ships), revise (fixable — say "
    "exactly what to fix), or escalate (needs a human). Be specific in 'reason'.\n\n"
    "Also record the *surprise* — the divergence between plan and outcome — as "
    "compact, decision-relevant signal (not a log):\n"
    "- expected: what the spec/plan intended.\n"
    "- observed: what the diff + tests actually produced.\n"
    "- cause: why they diverged (the root cause, one line).\n"
    "- fix: the concrete corrective the next attempt should apply (imperative). "
    "This is the most important field — it must be actionable, not a restatement "
    "of the problem.\n"
    "- task_type: a short slug for the kind of task (e.g. add-endpoint, bugfix, "
    "refactor, add-tests).\n"
    "- failure_mode: a short slug for how it fell short (e.g. missing-tests, "
    "empty-diff, wrong-api, incomplete, regression). \n"
    "On a clean pass with no divergence, leave these fields empty."
)


def _render_tests(results: list[TestResult]) -> str:
    if not results:
        return "(no test runs recorded)"
    last = results[-1]
    status = "PASSED" if last.passed else "FAILED"
    return f"Latest sandbox test run: {status}\n{last.summary}\n{last.raw}"


def _verifier_user(
    goal: str, criteria: str, diff: str, test_evidence: str
) -> str:
    """The verifier's user message — shared by the node and the eval predictor."""
    return (
        f"# Goal\n{goal}\n\n"
        f"# Acceptance criteria\n{criteria or '(none specified)'}\n\n"
        f"# Diff under review\n{diff or '(empty diff)'}\n\n"
        f"# Test evidence\n{test_evidence}"
    )


def make_verifier(
    gateway: Gateway | None = None,
    *,
    model: str | None = None,
    system_prompt: str = DEFAULT_VERIFIER_SYSTEM,
) -> Node:
    """Build the verifier node. With no ``gateway`` it returns a stub pass.

    ``system_prompt`` is injectable so an optimized judge prompt (self-improving
    §2) can be adopted without any orchestration change.
    """

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
        user = _verifier_user(
            spec.goal if spec else state.raw_request,
            criteria,
            state.code_diff or "",
            _render_tests(state.test_results),
        )
        try:
            verdict = gateway.structured(
                model=model, system=system_prompt, user=user, schema=Verdict
            )
        except GatewayError:
            logger.exception("Verifier gateway call failed; escalating.")
            verdict = Verdict(
                decision="escalate",
                reason="verifier model call failed — needs human review",
            )

        spent = usage_tokens(getattr(gateway, "last_usage", None))
        update: dict[str, object] = {
            "verdict": verdict,
            "status": "verifying",
            "tokens_used": state.tokens_used + spent,
        }
        if verdict.decision != "pass":
            update["reflections"] = [f"{verdict.decision}: {verdict.reason}"]
        return update

    return verifier


def verifier_predictor(
    gateway: Gateway,
    *,
    model: str,
    system_prompt: str = DEFAULT_VERIFIER_SYSTEM,
) -> Callable[[dict[str, Any]], str]:
    """The production predictor for the verifier eval / optimizer (§2).

    Adapts a labelled dataset case (``goal``/``acceptance_criteria``/``diff``/
    ``tests_passed``/``test_summary``) into the verifier's call and returns the
    decision string, so the eval harness scores the *real* judge behind an
    injected prompt — the seam GEPA optimizes.
    """

    def predict(inputs: dict[str, Any]) -> str:
        criteria = "\n".join(f"- {c}" for c in inputs.get("acceptance_criteria", []))
        passed = inputs.get("tests_passed")
        status = "PASSED" if passed else "FAILED"
        evidence = f"Latest sandbox test run: {status}\n{inputs.get('test_summary', '')}"
        user = _verifier_user(
            inputs.get("goal", ""), criteria, inputs.get("diff", ""), evidence
        )
        verdict = gateway.structured(
            model=model, system=system_prompt, user=user, schema=Verdict
        )
        return verdict.decision

    return predict


__all__ = ["DEFAULT_VERIFIER_SYSTEM", "make_verifier", "verifier_predictor"]
