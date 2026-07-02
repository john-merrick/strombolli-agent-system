"""Prompt node — translate the Spec into a coding prompt, traced via Notion.

Sits between Spec and Coding (PRD §6.2→§6.4). It turns the structured ``Spec``
into a precise, self-contained engineering prompt (one cheap free-text gateway
call, e.g. gemini-3.5-flash), **writes that prompt to the Notion ``Prompt``
field** so the exact instruction the coder receives is visible/traceable on the
task, and stashes it in ``state.plan`` for the coding node to build from.

With no gateway it falls back to a spec-derived prompt (offline/tests).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Protocol

from stromboli.llm.gateway import Gateway, GatewayError, usage_tokens
from stromboli.nodes.intake import Node
from stromboli.state import Spec, StromboliState

logger = logging.getLogger(__name__)

#: The Notion property the generated prompt is written to (PRD trace point).
DEFAULT_PROMPT_PROP = "Prompt"
#: Retrieves distilled lessons relevant to a task goal (design: context-as-state).
LessonRetriever = Callable[[str], Sequence[str]]

_SYSTEM = (
    "You translate a build specification into a single, precise, self-contained "
    "engineering prompt for an autonomous coding agent working in a git "
    "worktree. Output ONLY the prompt text — no preamble, no markdown fences. "
    "The prompt must state the concrete goal, the steps to take, the acceptance "
    "criteria to satisfy, any constraints, and instruct the agent to run the "
    "project's tests to verify before finishing."
)


class PromptNotion(Protocol):
    """The slice of the Notion client the prompt node needs (write the field)."""

    def set_rich_text(self, page_id: str, prop_name: str, text: str) -> None: ...


def _spec_block(spec: Spec | None, raw: str) -> str:
    if spec is None:
        return raw
    parts = [f"Goal: {spec.goal}"]
    if spec.acceptance_criteria:
        crit = "\n".join(f"- {c}" for c in spec.acceptance_criteria)
        parts.append(f"Acceptance criteria:\n{crit}")
    if spec.constraints:
        cons = "\n".join(f"- {c}" for c in spec.constraints)
        parts.append(f"Constraints:\n{cons}")
    if spec.affected_paths:
        paths = "\n".join(f"- {p}" for p in spec.affected_paths)
        parts.append(f"Likely affected paths:\n{paths}")
    return "\n\n".join(parts)


def _lessons_block(retriever: LessonRetriever | None, goal: str) -> str:
    """The retrieved-lessons context prepended to the planner input, or ""."""
    if retriever is None:
        return ""
    lessons = list(retriever(goal))
    if not lessons:
        return ""
    body = "\n".join(f"- {lesson}" for lesson in lessons)
    return (
        "Lessons from past similar tasks (apply where relevant, don't repeat "
        f"these mistakes):\n{body}"
    )


def make_prompt(
    gateway: Gateway | None = None,
    *,
    model: str | None = None,
    notion: PromptNotion | None = None,
    prop_name: str = DEFAULT_PROMPT_PROP,
    retriever: LessonRetriever | None = None,
) -> Node:
    """Build the prompt node. With no ``gateway`` it returns a spec-derived stub.

    When ``retriever`` is wired, distilled lessons from past similar tasks are
    injected into the planner's context (design: docs/design-context-as-state.md)
    — the cross-episode signal landing closest to where instructions are authored.
    """

    def prompt(state: StromboliState) -> dict[str, object]:
        goal = state.spec.goal if state.spec is not None else state.raw_request
        lessons = _lessons_block(retriever, goal)
        spec_text = _spec_block(state.spec, state.raw_request)
        if lessons:
            spec_text = f"{lessons}\n\n{spec_text}"
        spent = 0
        if gateway is None or model is None:
            generated = spec_text
        else:
            try:
                generated = gateway.complete(
                    model=model, system=_SYSTEM, user=spec_text
                )
            except GatewayError:
                logger.exception("Prompt gateway call failed; using spec text.")
                generated = spec_text
            spent = usage_tokens(getattr(gateway, "last_usage", None))

        # Write the prompt back to Notion for traceability (best-effort).
        if notion is not None and state.source == "notion":
            try:
                notion.set_rich_text(state.task_id, prop_name, generated)
            except Exception as exc:  # noqa: BLE001 - trace write must not crash
                logger.warning("Could not write Prompt for %s: %s", state.task_id, exc)

        return {"plan": generated, "tokens_used": state.tokens_used + spent}

    return prompt


__all__ = ["DEFAULT_PROMPT_PROP", "LessonRetriever", "PromptNotion", "make_prompt"]
