"""Graph nodes — one node per file (PRD §6).

Each node is a *factory* ``make_<node>(...)`` returning the LangGraph node
callable ``(StromboliState) -> dict`` (a partial state update). Factories let the
graph inject collaborators (LLM gateway, coder, sandbox, integrations) while
keeping each node a pure function of state + deps — unit-testable with fakes.

The two conditional edges (PRD §6.3 Router, §6.6 Verdict gate) live in
``router.py`` as plain routing functions, not nodes.
"""

from stromboli.nodes.coding import make_coding
from stromboli.nodes.human import make_human
from stromboli.nodes.intake import Node, make_intake
from stromboli.nodes.memory import make_memory_write
from stromboli.nodes.pr import make_pr
from stromboli.nodes.prompt import make_prompt
from stromboli.nodes.router import (
    make_route_after_verdict,
    route_after_coding,
    route_after_spec,
)
from stromboli.nodes.spec import make_spec
from stromboli.nodes.verifier import make_verifier

__all__ = [
    "Node",
    "make_coding",
    "make_human",
    "make_intake",
    "make_memory_write",
    "make_pr",
    "make_prompt",
    "make_route_after_verdict",
    "make_spec",
    "make_verifier",
    "route_after_coding",
    "route_after_spec",
]
