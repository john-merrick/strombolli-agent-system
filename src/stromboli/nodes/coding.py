"""Coding node (PRD §6.4) — the inner recursive write→test→fix loop.

The node calls the Agent SDK wrapper (:class:`~stromboli.llm.coder.AgentCoder`)
with the spec as the prompt and the sandboxed worktree as cwd; the SDK's agent
loop *is* the recursion. The node then runs the **sandbox test command** — its
only oracle — and appends the result. It never calls the verifier or judges its
own intent.

A non-clean SDK result (an execution error, not a bounded budget exit) raises
:class:`CoderError` so it surfaces as a node failure, never a silent pass. The
SDK's per-turn tool/usage records are nested as child spans under the coding
span (PRD §8), so this node self-traces rather than using the generic wrapper.

With no coder/sandbox/worktree wired it falls back to a stub (Phase 0 / tests).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from stromboli.llm.coder import Coder, CoderError, RateLimitError
from stromboli.llm.gateway import usage_tokens
from stromboli.nodes.intake import Node
from stromboli.observability.tracing import BuildTracer, NullTracer, traced_node
from stromboli.sandbox.runner import DEFAULT_TEST_COMMAND, TestSandbox, Worktree
from stromboli.state import StromboliState, TestResult

#: Resolves the prepared worktree for a task (clone-per-task, PRD §11.4). The
#: same worktree is returned across the coding/PR nodes of one task run.
WorktreeFor = Callable[[StromboliState], Worktree]


def _build_prompt(state: StromboliState) -> str:
    """Render the coder's prompt.

    Prefers the prompt-agent's output (``state.plan``, also written to Notion for
    traceability); falls back to the spec directly when no prompt was generated.
    Reviewer feedback is appended on a revise pass either way.
    """
    parts: list[str] = []
    if state.plan:
        parts.append(state.plan)
    else:
        parts.append(_spec_prompt(state))

    if state.verdict is not None and state.verdict.decision == "revise":
        parts.append(f"# Reviewer feedback (address this)\n{state.verdict.reason}")
    parts.append(
        "Implement the change in this repository and make the tests pass. Run the "
        "tests yourself to verify before finishing."
    )
    return "\n\n".join(parts)


def _spec_prompt(state: StromboliState) -> str:
    """Render the spec into a prompt (fallback when no generated prompt exists)."""
    spec = state.spec
    parts: list[str] = []
    if spec is not None:
        parts.append(f"# Goal\n{spec.goal}")
        if spec.acceptance_criteria:
            parts.append(
                "# Acceptance criteria\n"
                + "\n".join(f"- {c}" for c in spec.acceptance_criteria)
            )
        if spec.constraints:
            parts.append(
                "# Constraints\n" + "\n".join(f"- {c}" for c in spec.constraints)
            )
        if spec.affected_paths:
            parts.append(
                "# Likely affected paths\n"
                + "\n".join(f"- {p}" for p in spec.affected_paths)
            )
    else:
        parts.append(state.raw_request)
    return "\n\n".join(parts)


def make_coding(
    coder: Coder | None = None,
    sandbox: TestSandbox | None = None,
    worktree_for: WorktreeFor | None = None,
    *,
    tracer: BuildTracer | None = None,
    test_command: Sequence[str] = DEFAULT_TEST_COMMAND,
) -> Node:
    """Build the coding node. With no coder/sandbox/worktree it returns a stub."""
    tracer = tracer or NullTracer()

    def coding(state: StromboliState) -> dict[str, object]:
        if coder is None or sandbox is None or worktree_for is None:
            with traced_node(tracer, "coding", metadata={"task_id": state.task_id}):
                return {
                    "code_diff": "diff --git a/stub b/stub\n+stub",
                    "test_results": [
                        TestResult(passed=True, summary="stub: tests green")
                    ],
                    "inner_iterations": 1,
                    "session_id": "stub-session",
                    "status": "coding",
                }

        worktree = worktree_for(state)
        prompt = _build_prompt(state)
        with traced_node(
            tracer,
            "coding",
            metadata={"task_id": state.task_id, "resume": bool(state.session_id)},
        ) as span:
            try:
                run = coder.run(prompt, worktree.path, resume=state.session_id)
            except RateLimitError as exc:
                # Retryable escalation (PRD §4a): preserve the session so a resume
                # after the window continues this coding attempt rather than
                # starting cold. Routed to the human interrupt by route_after_coding.
                span.update(rate_limited=True, resets_at=exc.resets_at)
                reset = f" (resets at {exc.resets_at})" if exc.resets_at else ""
                return {
                    "status": "escalated",
                    "session_id": exc.session_id or state.session_id,
                    "reflections": [
                        f"rate-limited mid-run{reset}; resume the SDK session "
                        "after the window resets"
                    ],
                }
            # Nest each SDK turn as a child span (PRD §8) — ended immediately;
            # an unended observation is never exported.
            for turn in run.turn_records:
                span.child(
                    f"sdk-turn-{turn.index}",
                    metadata={"tools": list(turn.tools), "usage": turn.usage},
                ).end()
            span.update(turns=run.turns, subtype=run.subtype, cost_usd=run.cost_usd)
            if not run.clean:
                raise CoderError(
                    f"coder run not clean: subtype={run.subtype!r} "
                    f"is_error={run.is_error}"
                )
            sandbox_result = sandbox.run_tests(worktree.path, test_command)

            summary = (
                "tests passed"
                if sandbox_result.passed
                else f"tests failed (exit {sandbox_result.exit_code})"
            )
            # Prefer the SDK's cumulative usage; fall back to summing the turns.
            spent = usage_tokens(run.usage) or sum(
                usage_tokens(turn.usage) for turn in run.turn_records
            )
            update: dict[str, object] = {
                "code_diff": run.diff,
                "test_results": [
                    TestResult(
                        passed=sandbox_result.passed,
                        summary=summary,
                        raw=sandbox_result.output,
                    )
                ],
                "inner_iterations": run.turns,
                "session_id": run.session_id,
                "status": "coding",
                "tokens_used": state.tokens_used + spent,
            }
            # A revise re-entry is one completed outer-recursion cycle (§6.6).
            if state.verdict is not None and state.verdict.decision == "revise":
                update["outer_iterations"] = state.outer_iterations + 1
            span.update(
                output={
                    "tests_passed": sandbox_result.passed,
                    "code_diff_chars": len(run.diff),
                    "tokens_used": state.tokens_used + spent,
                }
            )
        return update

    return coding


__all__ = ["WorktreeFor", "make_coding"]
