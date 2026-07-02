"""Spec node (PRD §6.2) — turn the raw request into a structured ``Spec``.

A single structured call via the LiteLLM gateway (PRD §4), seeded with retrieved
semantic + episodic memory when a retriever is wired (Phase 4). It produces a
:class:`Spec` and must flag ``ambiguous=True`` when acceptance criteria can't be
pinned down (the Router reads that, §6.3).

With no gateway it falls back to a trivially-unambiguous stub so the graph flows
offline (Phase 0 / tests).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from stromboli.llm.gateway import Gateway, GatewayError, usage_tokens
from stromboli.nodes.intake import Node
from stromboli.state import Spec, StromboliState

logger = logging.getLogger(__name__)

#: Retrieves up to top-k memory snippets relevant to a request (PRD §7).
Retriever = Callable[[str], Sequence[str]]
#: Returns the project's context (e.g. its README/CLAUDE.md) for a task, or "".
ProjectContext = Callable[["StromboliState"], str]

_SYSTEM = (
    "You are the spec author for an autonomous coding system. Given a raw task "
    "request, produce a precise, buildable specification: a clear goal, concrete "
    "acceptance criteria, the file paths likely affected, and any constraints. "
    "If the request is too vague to pin down testable acceptance criteria, set "
    "ambiguous=true and explain what is unclear in the goal."
)


def make_spec(
    gateway: Gateway | None = None,
    *,
    model: str | None = None,
    retriever: Retriever | None = None,
    project_context: ProjectContext | None = None,
) -> Node:
    """Build the spec node. With no ``gateway`` it returns a stub spec.

    When ``project_context`` is wired, the project's conventions (e.g. its
    README/CLAUDE.md) are prepended so the Spec is written *with the project's
    context assumed* — sharpening acceptance criteria and resolving gaps that
    would otherwise be flagged ambiguous.
    """

    def spec(state: StromboliState) -> dict[str, object]:
        if gateway is None or model is None:
            produced = Spec(goal=state.raw_request, ambiguous=False)
            return {"spec": produced, "status": "specced"}

        project = project_context(state) if project_context is not None else ""
        project_block = f"\n\n{project}" if project else ""

        context = ""
        memory_refs: list[str] = []
        if retriever is not None:
            snippets = list(retriever(state.raw_request))
            memory_refs = snippets
            if snippets:
                context = "\n\nRelevant prior knowledge:\n" + "\n".join(
                    f"- {s}" for s in snippets
                )

        user = f"Task request:\n{state.raw_request}{project_block}{context}"
        try:
            produced = gateway.structured(
                model=model, system=_SYSTEM, user=user, schema=Spec
            )
        except GatewayError:
            logger.exception("Spec gateway call failed; flagging ambiguous.")
            produced = Spec(
                goal=state.raw_request,
                ambiguous=True,
                constraints=["spec generation failed — needs human review"],
            )
        spent = usage_tokens(getattr(gateway, "last_usage", None))
        return {
            "spec": produced,
            "status": "specced",
            "memory_refs": memory_refs,
            "tokens_used": state.tokens_used + spent,
        }

    return spec


__all__ = ["ProjectContext", "Retriever", "make_spec"]
