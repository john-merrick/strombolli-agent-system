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
from collections.abc import Iterator
from contextlib import contextmanager
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
from stromboli.integrations.github import GitHubGateway, GitRunner
from stromboli.integrations.notion import (
    AppendGateway,
    build_feedback_summary,
    resilient_append,
)
from stromboli.integrations.telegram import Notifier, NullNotifier
from stromboli.llm.coder import Coder
from stromboli.llm.gateway import Gateway
from stromboli.memory import Memory
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
    route_after_coding,
)
from stromboli.nodes.coding import WorktreeFor
from stromboli.nodes.intake import NotionReader
from stromboli.nodes.router import CODING, HUMAN, PR, VERIFIER, route_after_spec
from stromboli.observability.tracing import BuildTracer, NullTracer, traced_node
from stromboli.sandbox.runner import TestSandbox, Worktree
from stromboli.settings import Settings, load_settings
from stromboli.state import Source, StromboliState

logger = logging.getLogger(__name__)


class NotionGateway(NotionReader, AppendGateway, Protocol):
    """The combined Notion surface the graph needs (read, append, set status)."""

    def update_task(self, page_id: str, *, status: str | None = ...) -> None: ...


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
    #: Coder + sandbox + per-task worktree resolver (Phase 2). All three must be
    #: set together to enable the real coding node; else it runs the stub.
    coder: Coder | None = None
    sandbox: TestSandbox | None = None
    worktree_for: WorktreeFor | None = None
    #: The three-tier memory (Phase 4): seeds Spec, learns on completion.
    memory: Memory | None = None
    #: GitHub gateway for the live PR node (Phase 6).
    github: GitHubGateway | None = None
    #: Git runner seam for the PR node (``None`` → real git); injected in tests.
    git_run: GitRunner | None = None
    base_branch: str = "main"
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


def _untraced(node: Node) -> Any:
    """Pass a self-tracing node to ``add_node`` (returns ``Any``, see _traced)."""
    return node


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
    retriever = deps.memory.recall_for_spec if deps.memory is not None else None
    builder.add_node(
        "spec",
        _traced(
            deps.tracer,
            "spec",
            make_spec(deps.gateway, model=deps.reasoning_model, retriever=retriever),
        ),
    )
    # The coding node self-traces (it nests the SDK turns as child spans, §8).
    builder.add_node(
        "coding",
        _untraced(
            make_coding(
                deps.coder,
                deps.sandbox,
                deps.worktree_for,
                tracer=deps.tracer,
            )
        ),
    )
    builder.add_node(
        "verifier",
        _traced(
            deps.tracer,
            "verifier",
            make_verifier(deps.gateway, model=deps.verifier_model),
        ),
    )
    builder.add_node(
        "pr",
        _traced(
            deps.tracer,
            "pr",
            make_pr(
                github=deps.github,
                notion=deps.notion,
                worktree_for=deps.worktree_for,
                base=deps.base_branch,
                dry_run=deps.dry_run_pr,
                git_run=deps.git_run,
            ),
        ),
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
        "memory", _traced(deps.tracer, "memory", make_memory_write(deps.memory))
    )

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "spec")
    builder.add_conditional_edges(
        "spec", route_after_spec, {CODING: "coding", HUMAN: "human"}
    )
    # A rate-limit cutoff escalates to the human interrupt (PRD §4a); otherwise
    # coding proceeds to verification.
    builder.add_conditional_edges(
        "coding", route_after_coding, {VERIFIER: "verifier", HUMAN: "human"}
    )
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

    # Caller-supplied deps → fully offline (tests / Phase 0 stub).
    if deps is not None:
        return _execute(deps, resolved_id, raw_request, source, checkpointer)

    resolved_settings = settings or load_settings()
    deps = _deps_from_settings(resolved_settings)

    # A Notion-sourced task gets a clone-per-task worktree (PRD §11.4) so the
    # coding + PR nodes operate on an isolated checkout, cleaned up on exit.
    if source == "notion" and deps.notion is not None:
        with _provision_worktree(resolved_settings, deps.notion, resolved_id) as wt:
            deps.worktree_for = lambda _s: wt
            return _execute(deps, resolved_id, raw_request, source, checkpointer)

    return _execute(deps, resolved_id, raw_request, source, checkpointer)


def _execute(
    deps: GraphDeps,
    task_id: str,
    raw_request: str,
    source: Source,
    checkpointer: Any | None,
) -> StromboliState:
    """Compile + invoke the graph for one task, trace it, and finalize."""
    graph = build_graph(deps, checkpointer=checkpointer or MemorySaver())
    initial = StromboliState(task_id=task_id, source=source, raw_request=raw_request)

    deps.tracer.start(task_id=task_id, name=raw_request[:60] or "task")
    error: str | None = None
    result: Any = None
    try:
        result = graph.invoke(
            initial, config={"configurable": {"thread_id": task_id}}
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        deps.tracer.tag(_terminal_tag(result if error is None else None))
        deps.tracer.finish(error=error)

    # A paused (interrupted) run isn't terminal — the escalation was already
    # surfaced by the human node; don't finalize it as done.
    if isinstance(result, dict) and "__interrupt__" in result:
        return StromboliState.model_validate(
            {k: v for k, v in result.items() if k != "__interrupt__"}
        )

    final = StromboliState.model_validate(result)
    _finalize(final, deps)
    return final


def _finalize(state: StromboliState, deps: GraphDeps) -> None:
    """Terminal I/O for a completed task: Notion write-back + Telegram (PRD §6.7)."""
    if state.status != "done":
        return
    if deps.notion is not None:
        summary = build_feedback_summary(
            status="done",
            pr_url=state.pr_url,
            reflections=state.reflections,
            coverage_note=state.verdict.coverage_note if state.verdict else "",
        )
        resilient_append(deps.notion, state.task_id, summary)
        try:
            # Agent's work is done (a verified PR is open for human merge) →
            # Complete on the board (To do → Working on → Complete).
            deps.notion.update_task(state.task_id, status="Complete")
        except Exception as exc:  # noqa: BLE001 - write-back must never crash a run
            logger.warning("Notion status write failed for %s: %s", state.task_id, exc)
    deps.notifier.done(state.task_id, state.pr_url)


@contextmanager
def _provision_worktree(
    settings: Settings, notion: NotionGateway, task_id: str
) -> Iterator[Worktree]:
    """Clone-per-task worktree for a Notion task, cleaned up on exit (PRD §11.4)."""
    from stromboli.sandbox.runner import WorktreeManager

    task = notion.get_task(task_id)
    repo = notion.get_project_repo(task)  # type: ignore[attr-defined]
    manager = WorktreeManager(settings.workspace_root, token=settings.github_token)
    with manager.worktree(repo, task.page_id, task.name) as worktree:
        yield worktree


def _deps_from_settings(settings: Settings) -> GraphDeps:
    """Assemble the production dependency graph from env-backed settings."""
    from stromboli.integrations.github import GitHubClient
    from stromboli.integrations.notion import NotionTaskClient
    from stromboli.integrations.telegram import make_notifier
    from stromboli.llm.coder import AgentCoder
    from stromboli.llm.gateway import build_gateway
    from stromboli.observability.tracing import build_tracer
    from stromboli.sandbox.runner import SandboxRunner

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
    memory = Memory.open(settings.chroma_persist_dir)
    coder = AgentCoder(
        model=config.models.coder,
        api_key=settings.anthropic_api_key,
        auth_mode=config.auth_mode,
        max_turns=config.budgets.max_inner_turns,
    )
    return GraphDeps(
        budgets=config.budgets,
        tracer=tracer,
        gateway=gateway,
        reasoning_model=config.models.reasoning,
        verifier_model=config.models.verifier,
        notion=notion,
        notifier=notifier,
        memory=memory,
        coder=coder,
        sandbox=SandboxRunner(),
        github=GitHubClient(settings.github_token),
        base_branch="main",
        dry_run_pr=False,
    )


def _terminal_tag(result: Any | None) -> str:
    """A best-effort terminal-status tag for the trace."""
    if result is None:
        return "failure"
    status = result.get("status") if isinstance(result, dict) else None
    return str(status or "unknown")


__all__ = ["GraphDeps", "build_graph", "run_task"]
