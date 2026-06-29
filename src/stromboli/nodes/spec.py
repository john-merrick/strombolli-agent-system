"""Spec node (PRD §6.2) — turn the raw request into a structured ``Spec``.

Real behavior (Phase 1): a single structured call via the LiteLLM gateway,
seeded with retrieved semantic + episodic memory, producing a :class:`Spec` and
flagging ``ambiguous=True`` when acceptance criteria can't be pinned down.

Phase 0 stub: synthesize a trivially-unambiguous spec from the raw request so the
graph flows end-to-end without an LLM call.
"""

from __future__ import annotations

from collections.abc import Callable

from stromboli.nodes.intake import Node
from stromboli.state import Spec, StromboliState

#: Produces a structured Spec from a free-text request (the gateway, Phase 1).
SpecFn = Callable[[str], Spec]


def make_spec(spec_fn: SpecFn | None = None) -> Node:
    """Build the spec node. ``spec_fn`` is the gateway call; ``None`` → stub."""

    def spec(state: StromboliState) -> dict[str, object]:
        if spec_fn is None:
            produced = Spec(goal=state.raw_request, ambiguous=False)
        else:
            produced = spec_fn(state.raw_request)
        return {"spec": produced, "status": "specced"}

    return spec


__all__ = ["SpecFn", "make_spec"]
