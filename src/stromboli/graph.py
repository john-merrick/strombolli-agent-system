"""Build and compile the Stromboli ``StateGraph`` (PRD §3 / §6).

This is the orchestration core: it wires the named nodes, the two conditional
edges (Router §6.3, Verdict gate §6.6), the revise cycle, and the checkpointer
into a compiled LangGraph, then drives one task through it.

The graph owns *everything around* the coding node — orchestration, budgets,
verification, memory, tracing — while the coding node embeds the Agent SDK loop
(PRD §1). Collaborators reach the nodes through :class:`GraphDeps`, so the whole
graph is constructed from injected seams and unit-testable with fakes.

Tracing: every node is wrapped so it emits a Langfuse span under a single
per-task trace correlated by ``task_id`` (PRD §8); :func:`run_task` opens the
trace before invoking the graph and closes it after.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from stromboli.config import (
    DEFAULT_REASONING_MODEL,
    DEFAULT_VERIFIER_MODEL,
    Budgets,
    from_settings,
)
from stromboli.integrations.notion import AppendGateway
from stromboli.integrations.telegram import Notifier, NullNotifier
from stromboli.llm.gateway import Gateway
from stromboli.nodes import (
    Node,
    make_coding,
    make_human,
    make_intake,
    make_memory_write,
    make_pr,
    make_route_after_verdict,
    make_spec,
    make_verifier,
)
from stromboli.nodes.intake import NotionReader
from stromboli.nodes.router import CODING, HUMAN, PR, route_after_spec
from stromboli.observability.tracing import BuildTracer, NullTracer, traced_node
from stromboli.settings import Settings, load_settings
from stromboli.state import Source, StromboliState

logger = logging.getLogger(__name__)


class NotionGateway(NotionReader, AppendGateway, Protocol):
    """The combined Notion surface the graph needs (read a task + append back)."""


@dataclass
class GraphDeps:
    """The collaborators the graph injects into its nodes.

    Phase 0 needed only budgets + tracer (stub nodes). Phase 1 adds the LiteLLM
    gateway (spec), the Notion surface (intake + escalation write-back), and the
    Telegram notifier (escalations). The coder + sandbox (Phase 2) and memory
    (Phase 4) slot in here without changing the graph's shape.
    """

    budgets: Budgets = field(default_factory=Budgets)
    tracer: BuildTracer = field(default_factory=NullTracer)
    gateway: Gateway | None = None
    reasoning_model: str = DEFAULT_REASONING_MODEL
    verifier_model: str = DEFAULT_VERIFIER_MODEL
    notion: NotionGateway | None = None
    notifier: Notifier = field(default_factory=NullNotifier)
    #: PR node opens no real PR while true (Phase 0/1 default; live in Phase 6).
    dry_run_pr: bool = True


def _traced(tracer: BuildTracer, name: str, node: Node) -> Any:
    """Wrap a node so it emits a Langfuse span on each invocation (PRD §8).

    Returns ``Any`` because LangGraph's overloaded ``add_node`` cannot infer its
    node-input type var from an alias-typed ``Callable`` value (only from a
    literal function) — the wrapper itself stays fully typed internally.
    """

    def wrapped(state: StromboliState) -> dict[str, object]:
        with traced_node(tracer, name, metadata={"task_id": state.task_id}):
            return node(state)

    return wrapped


def build_graph(deps: GraphDeps, *, checkpointer: Any | None = None) -> Any:
    """Assemble and compile the StateGraph from ``deps``.

    Edges (PRD §3):
        START → intake → spec
        spec  ─(router §6.3)→ coding | human
        coding → verifier
        verifier ─(verdict gate §6.6)→ pr | coding | human
        pr → memory → END
        human → END
    """
    builder = StateGraph(StromboliState)

    builder.add_node(
        "intake", _traced(deps.tracer, "intake", make_intake(notion=deps.notion))
    )
    builder.add_node(
        "spec",
        _traced(
            deps.tracer,
            "spec",
            make_spec(deps.gateway, model=deps.reasoning_model),
        ),
    )
    builder.add_node("coding", _traced(deps.tracer, "coding", make_coding()))
    builder.add_node("verifier", _traced(deps.tracer, "verifier", make_verifier()))
    builder.add_node(
        "pr", _traced(deps.tracer, "pr", make_pr(dry_run=deps.dry_run_pr))
    )
    builder.add_node(
        "human",
        _traced(
            deps.tracer,
            "human",
            make_human(notifier=deps.notifier, notion=deps.notion),
        ),
    )
    builder.add_node(
        "memory", _traced(deps.tracer, "memory", make_memory_write())
    )

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "spec")
    builder.add_conditional_edges(
        "spec", route_after_spec, {CODING: "coding", HUMAN: "human"}
    )
    builder.add_edge("coding", "verifier")
    builder.add_conditional_edges(
        "verifier",
        make_route_after_verdict(deps.budgets),
        {PR: "pr", CODING: "coding", HUMAN: "human"},
    )
    builder.add_edge("pr", "memory")
    builder.add_edge("memory", END)
    builder.add_edge("human", END)

    return builder.compile(checkpointer=checkpointer)


def run_task(
    raw_request: str,
    *,
    source: Source = "cli",
    task_id: str | None = None,
    settings: Settings | None = None,
    deps: GraphDeps | None = None,
    checkpointer: Any | None = None,
) -> StromboliState:
    """Run one task end-to-end through the graph and return its final state.

    Opens a single Langfuse trace correlated by ``task_id`` (PRD §8), invokes the
    compiled graph under a checkpointer thread, and closes the trace. When
    ``deps`` is supplied the run is fully offline (no settings/Langfuse needed) —
    that is the path tests and the Phase 0 stub use.
    """
    resolved_id = task_id or uuid.uuid4().hex

    if deps is None:
        deps = _deps_from_settings(settings or load_settings())

    graph = build_graph(deps, checkpointer=checkpointer or MemorySaver())
    initial = StromboliState(
        task_id=resolved_id, source=source, raw_request=raw_request
    )

    deps.tracer.start(task_id=resolved_id, name=raw_request[:60] or "task")
    error: str | None = None
    result: Any = None
    try:
        result = graph.invoke(
            initial, config={"configurable": {"thread_id": resolved_id}}
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        deps.tracer.tag(_terminal_tag(result if error is None else None))
        deps.tracer.finish(error=error)

    return StromboliState.model_validate(result)


def _deps_from_settings(settings: Settings) -> GraphDeps:
    """Assemble the production dependency graph from env-backed settings."""
    from stromboli.integrations.notion import NotionTaskClient
    from stromboli.integrations.telegram import make_notifier
    from stromboli.llm.gateway import build_gateway
    from stromboli.observability.tracing import build_tracer

    config = from_settings(settings)
    tracer = build_tracer(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
    gateway = build_gateway(
        base_url=settings.litellm_base_url, api_key=settings.litellm_api_key
    )
    notion = NotionTaskClient(settings.notion_token)
    notifier = make_notifier(settings.telegram_bot_token, settings.telegram_chat_id)
    return GraphDeps(
        budgets=config.budgets,
        tracer=tracer,
        gateway=gateway,
        reasoning_model=config.models.reasoning,
        verifier_model=config.models.verifier,
        notion=notion,
        notifier=notifier,
    )


def _terminal_tag(result: Any | None) -> str:
    """A best-effort terminal-status tag for the trace."""
    if result is None:
        return "failure"
    status = result.get("status") if isinstance(result, dict) else None
    return str(status or "unknown")


__all__ = ["GraphDeps", "build_graph", "run_task"]
