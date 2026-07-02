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
from pathlib import Path
from typing import Any, Protocol

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from stromboli.config import (
    DEFAULT_PROMPT_MODEL,
    DEFAULT_REASONING_MODEL,
    DEFAULT_VERIFIER_MODEL,
    Budgets,
    from_settings,
)
from stromboli.integrations.github import GitHubGateway, GitRunner
from stromboli.integrations.notion import (
    STATUS_WORKING,
    AppendGateway,
    Repo,
    build_feedback_summary,
    resilient_append,
)
from stromboli.integrations.telegram import Notifier, NullNotifier
from stromboli.llm.coder import Coder, TurnRecord
from stromboli.llm.gateway import Gateway
from stromboli.memory import Memory
from stromboli.nodes import (
    Node,
    make_coding,
    make_human,
    make_intake,
    make_memory_write,
    make_pr,
    make_prompt,
    make_route_after_verdict,
    make_spec,
    make_verifier,
    route_after_coding,
)
from stromboli.nodes.coding import WorktreeFor
from stromboli.nodes.intake import NotionReader
from stromboli.nodes.router import (
    CODING,
    HUMAN,
    MEMORY,
    PR,
    PROMPT,
    VERIFIER,
    route_after_pr,
    route_after_spec,
)
from stromboli.observability.runtrace import FileRunTrace, RunTrace, summarize_output
from stromboli.observability.tracing import BuildTracer, NullTracer, traced_node
from stromboli.sandbox.runner import GitError, TestSandbox, Worktree
from stromboli.settings import Settings, load_settings
from stromboli.state import Source, StromboliState

logger = logging.getLogger(__name__)


class NotionGateway(NotionReader, AppendGateway, Protocol):
    """The combined Notion surface the graph needs (read, append, status, prompt)."""

    def update_task(self, page_id: str, *, status: str | None = ...) -> None: ...

    def set_rich_text(self, page_id: str, prop_name: str, text: str) -> None: ...


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
    prompt_model: str = DEFAULT_PROMPT_MODEL
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
    #: Project context loader for the Spec node — returns the project's
    #: README/CLAUDE.md (from its Notion ``Context Root``) or "". ``None`` → off.
    project_context: Any = None
    #: Where per-run trace files are written (None → no local trace file).
    workspace_root: Path | None = None
    #: GitHub gateway for the live PR node (Phase 6).
    github: GitHubGateway | None = None
    #: Git runner seam for the PR node (``None`` → real git); injected in tests.
    git_run: GitRunner | None = None
    base_branch: str = "main"
    #: PR node opens no real PR while true (Phase 0/1 default; live in Phase 6).
    dry_run_pr: bool = True
    #: Paused-task index for the investigate loop (suspend/resume). ``None`` →
    #: built lazily from ``workspace_root`` when a task suspends; inject in tests.
    paused_index: Any = None
    #: Sender for the dedicated investigate-bot — used to post the opener when a
    #: task suspends. ``None`` → no opener sent (e.g. offline/tests).
    investigate_notify: Any = None
    #: Remove a task's durable worktree on a terminal outcome — ``Callable[[str],
    #: None]`` keyed by task_id. ``None`` → no cleanup (tests/offline). Not called
    #: on suspend, so the worktree survives for resume/probe.
    worktree_cleanup: Any = None
    #: Registers an opened PR for the feedback loop (design: PR feedback loop).
    #: ``(state, worktree, pr_url, pr_number) -> None``. ``None`` → not tracked.
    on_published: Any = None
    #: Records a terminal verdict to the failure store (self-improving §1).
    #: ``(state) -> None``, called for every terminal run. ``None`` → not captured.
    on_terminal: Any = None


def _wrap(
    name: str,
    node: Node,
    *,
    tracer: BuildTracer | None,
    run_trace: RunTrace,
) -> Any:
    """Wrap a node with a Langfuse span (unless ``tracer is None`` for
    self-tracing nodes) + a local run-trace record.

    Returns ``Any`` because LangGraph's overloaded ``add_node`` can't infer its
    node-input type var from an alias-typed ``Callable`` (only a literal func).
    """

    def wrapped(state: StromboliState) -> dict[str, object]:
        if tracer is not None:
            with traced_node(tracer, name, metadata={"task_id": state.task_id}) as span:
                span.update(
                    input={
                        "status": state.status,
                        "outer_iterations": state.outer_iterations,
                        "tokens_used": state.tokens_used,
                    }
                )
                out = node(state)
                # A node's returned partial state IS the state diff (PRD §5) —
                # surface it as the span's output so Langfuse answers "what did
                # this node change and why did the graph route as it did".
                span.update(output=summarize_output(out))
        else:  # self-tracing node (coding nests its own SDK-turn spans)
            out = node(state)
        run_trace.record(name, out)
        return out

    return wrapped


def build_graph(
    deps: GraphDeps,
    *,
    checkpointer: Any | None = None,
    run_trace: RunTrace | None = None,
) -> Any:
    """Assemble and compile the StateGraph from ``deps``.

    Edges (PRD §3):
        START → intake → spec
        spec  ─(router §6.3)→ prompt | human
        prompt → coding
        coding ─→ verifier | human (rate-limit escalation, §4a)
        verifier ─(verdict gate §6.6)→ pr | coding | human
        pr ─→ memory | human (publication failure)
        memory → END
        human → END
    """
    trace = run_trace or RunTrace()
    builder = StateGraph(StromboliState)

    def traced(name: str, node: Node) -> Any:
        return _wrap(name, node, tracer=deps.tracer, run_trace=trace)

    builder.add_node("intake", traced("intake", make_intake(notion=deps.notion)))
    retriever = deps.memory.recall_for_spec if deps.memory is not None else None
    builder.add_node(
        "spec",
        traced(
            "spec",
            make_spec(
                deps.gateway, model=deps.reasoning_model, retriever=retriever,
                project_context=deps.project_context,
            ),
        ),
    )
    lesson_retriever = (
        deps.memory.recall_lessons if deps.memory is not None else None
    )
    builder.add_node(
        "prompt",
        traced(
            "prompt",
            make_prompt(
                deps.gateway, model=deps.prompt_model, notion=deps.notion,
                retriever=lesson_retriever,
            ),
        ),
    )
    # The coding node self-traces its SDK turns as Langfuse child spans (§8); we
    # still record its output to the local run-trace.
    builder.add_node(
        "coding",
        _wrap(
            "coding",
            make_coding(
                deps.coder,
                deps.sandbox,
                deps.worktree_for,
                tracer=deps.tracer,
            ),
            tracer=None,  # coding self-traces its SDK turns (§8)
            run_trace=trace,
        ),
    )
    builder.add_node(
        "verifier",
        traced("verifier", make_verifier(deps.gateway, model=deps.verifier_model)),
    )
    builder.add_node(
        "pr",
        traced(
            "pr",
            make_pr(
                github=deps.github,
                notion=deps.notion,
                worktree_for=deps.worktree_for,
                base=deps.base_branch,
                dry_run=deps.dry_run_pr,
                git_run=deps.git_run,
                on_published=deps.on_published,
            ),
        ),
    )
    builder.add_node(
        "human", traced("human", make_human(notifier=deps.notifier, notion=deps.notion))
    )
    builder.add_node("memory", traced("memory", make_memory_write(deps.memory)))

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "spec")
    # Ready spec → prompt agent → coding; ambiguous spec → human (skips prompt).
    builder.add_conditional_edges(
        "spec", route_after_spec, {PROMPT: "prompt", HUMAN: "human"}
    )
    builder.add_edge("prompt", "coding")
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
    # A PR publication failure (after a verified diff) parks with a human
    # rather than proceeding to the memory write.
    builder.add_conditional_edges(
        "pr", route_after_pr, {MEMORY: "memory", HUMAN: "human"}
    )
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
    repo: str | None = None,
    dry_run_pr: bool | None = None,
) -> StromboliState:
    """Run one task end-to-end through the graph and return its final state.

    Opens a single Langfuse trace correlated by ``task_id`` (PRD §8), invokes the
    compiled graph under a checkpointer thread, and closes the trace. When
    ``deps`` is supplied the run is fully offline (no settings/Langfuse needed) —
    that is the path tests and the Phase 0 stub use.

    ``repo`` (CLI source only) names the target repository — a local path or a
    GitHub ``owner/name`` — a clone-per-task worktree is provisioned from, so a
    CLI task reaches the real coding node without Notion. ``dry_run_pr`` (when
    not ``None``) overrides whether the PR node opens a real PR.
    """
    resolved_id = task_id or uuid.uuid4().hex

    # Caller-supplied deps → fully offline (tests / Phase 0 stub).
    if deps is not None:
        return _execute(deps, resolved_id, raw_request, source, checkpointer)

    resolved_settings = settings or load_settings()
    deps = _deps_from_settings(resolved_settings)
    if dry_run_pr is not None:
        deps.dry_run_pr = dry_run_pr

    # A Notion-sourced task gets a clone-per-task worktree (PRD §11.4) so the
    # coding + PR nodes operate on an isolated checkout, cleaned up on exit.
    if source == "notion" and deps.notion is not None:
        # Claim the task on the board BEFORE the (slow) clone/worktree setup.
        # The watcher's dispatch guard is Status == To do; writing Working at
        # the intake node — i.e. *after* provisioning — leaves a window as long
        # as a full clone in which a concurrent poll double-dispatches the task
        # (the loser then collides on the worktree branch and escalates).
        try:
            deps.notion.update_task(resolved_id, status=STATUS_WORKING)
        except Exception as exc:  # noqa: BLE001 - claim is best-effort
            logger.warning("Could not claim task %s: %s", resolved_id, exc)
        try:
            with _provision_worktree(resolved_settings, deps.notion, resolved_id) as wt:
                deps.worktree_for = lambda _s: wt
                return _execute(deps, resolved_id, raw_request, source, checkpointer)
        except (ValueError, GitError) as exc:
            # Can't resolve/clone the repo (e.g. no Project relation set) — this
            # isn't a crash, it's a task that needs a human: escalate gracefully.
            return _escalate_unbuildable(deps, resolved_id, raw_request, str(exc))

    # A CLI-sourced task with an explicit --repo gets the same clone-per-task
    # worktree, resolved directly (no Notion). A bad repo argument raises — the
    # operator is at the terminal, so fail fast rather than escalate.
    if source == "cli" and repo is not None:
        from stromboli.sandbox.runner import WorktreeManager

        manager = WorktreeManager(
            resolved_settings.workspace_root, token=resolved_settings.github_token
        )
        with manager.worktree(_parse_repo_arg(repo), resolved_id, raw_request) as wt:
            deps.worktree_for = lambda _s: wt
            return _execute(deps, resolved_id, raw_request, source, checkpointer)

    return _execute(deps, resolved_id, raw_request, source, checkpointer)


def _parse_repo_arg(arg: str) -> Repo:
    """``--repo``: a local path (cloned directly) or a GitHub ``owner/name``."""
    path = Path(arg).expanduser()
    if path.exists():
        resolved = path.resolve()
        return Repo(owner="local", repo=resolved.name, source=str(resolved))
    owner, sep, name = arg.partition("/")
    if sep and owner and name and "/" not in name:
        return Repo(owner=owner, repo=name.removesuffix(".git"))
    raise ValueError(
        f"--repo must be an existing local path or 'owner/name', got {arg!r}"
    )


def _escalate_unbuildable(
    deps: GraphDeps, run_id: str, raw_request: str, reason: str
) -> StromboliState:
    """A task that can't even be set up (no repo) → Review + Telegram, not a crash."""
    logger.warning("Task %s is unbuildable: %s", run_id, reason)
    note = f"can't build — {reason}"
    if deps.notion is not None:
        resilient_append(deps.notion, run_id, f"🚨 **Stromboli could not start:** {note}")
        try:
            deps.notion.update_task(run_id, status="Review")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Notion status write failed for %s: %s", run_id, exc)
    deps.notifier.escalation(run_id, note)
    return StromboliState(
        task_id=run_id, source="notion", raw_request=raw_request, status="escalated"
    )


def _execute(
    deps: GraphDeps,
    task_id: str,
    raw_request: str,
    source: Source,
    checkpointer: Any | None,
) -> StromboliState:
    """Compile + invoke the graph for one task, trace it, and finalize."""
    run_trace: RunTrace = (
        FileRunTrace(deps.workspace_root, task_id)
        if deps.workspace_root is not None
        else RunTrace()
    )
    if isinstance(run_trace, FileRunTrace):
        logger.info("Run trace: %s", run_trace.directory)

    graph = build_graph(
        deps,
        checkpointer=checkpointer or MemorySaver(),
        run_trace=run_trace,
    )
    initial = StromboliState(task_id=task_id, source=source, raw_request=raw_request)
    deps.tracer.start(task_id=task_id, name=raw_request[:60] or "task")

    try:
        result = graph.invoke(
            initial, config={"configurable": {"thread_id": task_id}}
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        deps.tracer.tag("failure")
        deps.tracer.finish(error=error)
        raise

    # A paused (interrupted) run isn't terminal — the escalation was already
    # surfaced by the human node; don't finalize it as done.
    if isinstance(result, dict) and "__interrupt__" in result:
        deps.tracer.tag("escalated")
        deps.tracer.finish()
        paused = StromboliState.model_validate(
            {k: v for k, v in result.items() if k != "__interrupt__"}
        )
        _capture_terminal(paused, deps)
        return paused

    deps.tracer.tag(_terminal_tag(result))
    deps.tracer.finish()
    final = StromboliState.model_validate(result)
    _finalize(final, deps)
    _capture_terminal(final, deps)
    return final


def _capture_terminal(state: StromboliState, deps: GraphDeps) -> None:
    """Record the terminal verdict for learning (self-improving §1), best-effort.

    Fires for *every* terminal run — a verified pass and a rejection alike — so
    the verifier's rejection signal is captured durably instead of discarded.
    """
    if deps.on_terminal is None:
        return
    try:
        deps.on_terminal(state)
    except Exception as exc:  # noqa: BLE001 - capture must never crash a run
        logger.warning("Failure capture failed for %s: %s", state.task_id, exc)


def _finalize(state: StromboliState, deps: GraphDeps) -> None:
    """Terminal I/O for a completed task: Notion write-back + Telegram (PRD §6.7)."""
    if state.status != "done":
        return
    # Only a Notion-sourced task has a page to write back to.
    if deps.notion is not None and state.source == "notion":
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


def _durable_worktree_for(manager: Any, notion: Any) -> WorktreeFor:
    """A durable ``worktree_for`` for the phases path: ensure (don't remove)."""

    def worktree_for(state: StromboliState) -> Worktree:
        task = notion.get_task(state.task_id)
        repo = notion.get_project_repo(task)
        worktree: Worktree = manager.ensure(repo, task.page_id, task.name)
        return worktree

    return worktree_for


def _worktree_remover(manager: Any, notion: Any) -> Any:
    """Build a ``Callable[[str], None]`` that tears down a task's worktree."""

    def remove(task_id: str) -> None:
        try:
            task = notion.get_task(task_id)
            repo = notion.get_project_repo(task)
            manager.remove(repo, task.page_id, task.name)
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            logger.warning("Worktree cleanup failed for %s", task_id)

    return remove


def _log_coder_turn(record: TurnRecord) -> None:
    """Live per-turn visibility: one log line as each SDK turn streams."""
    out_tokens = (record.usage or {}).get("output_tokens")
    logger.info(
        "coding turn %d: tools=%s output_tokens=%s",
        record.index,
        list(record.tools),
        out_tokens,
    )


def _deps_from_settings(settings: Settings) -> GraphDeps:
    """Assemble the production dependency graph from env-backed settings."""
    from stromboli.integrations.github import GitHubClient
    from stromboli.integrations.notion import NotionTaskClient
    from stromboli.integrations.project_context import (
        github_file_fetcher,
        make_project_context,
    )
    from stromboli.integrations.telegram import make_notifier, telegram_sender
    from stromboli.llm.coder import AgentCoder
    from stromboli.llm.gateway import build_gateway
    from stromboli.observability.tracing import build_tracer
    from stromboli.orchestration.paused import PausedIndex
    from stromboli.sandbox.runner import SandboxRunner, WorktreeManager

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
    # The investigate loop: persist suspended state + post the opener on the
    # dedicated investigate-bot (None when that bot/chat isn't configured).
    paused_index = PausedIndex(settings.workspace_root / ".stromboli" / "paused.db")
    investigate_notify = (
        telegram_sender(
            settings.telegram_investigate_bot_token, settings.telegram_chat_id
        )
        if settings.telegram_investigate_bot_token and settings.telegram_chat_id
        else None
    )
    memory = Memory.open(settings.chroma_persist_dir)
    # Spec-node project context: fetch the project's Context Root file (README /
    # CLAUDE.md) from GitHub so tasks are specced with the project assumed.
    project_context = make_project_context(
        notion, fetch=github_file_fetcher(settings.github_token)
    )
    # Durable per-task worktrees for the phases/Prefect path (run_task overrides
    # worktree_for with its own context-managed provisioning). Kept across a
    # suspend so resume/probe rebuild on the coder's prior work; removed by
    # finalize on a terminal outcome (and by the investigate sweeper on expiry).
    worktree_manager = WorktreeManager(
        settings.workspace_root, token=settings.github_token
    )
    worktree_for = _durable_worktree_for(worktree_manager, notion)
    worktree_cleanup = _worktree_remover(worktree_manager, notion)
    coder = AgentCoder(
        model=config.models.coder,
        api_key=settings.anthropic_api_key,
        auth_mode=config.auth_mode,
        max_turns=config.budgets.max_inner_turns,
        on_turn=_log_coder_turn,
    )
    github = GitHubClient(settings.github_token)
    # Register each opened PR into the feedback-loop index (§ PR feedback loop):
    # CI failures + review comments then drive bounded fix cycles on the sweep.
    pr_index = _open_pr_index(settings.workspace_root)
    # Capture every terminal verdict for the self-improvement loops (§1).
    failure_index = _open_failure_index(settings.workspace_root)
    return GraphDeps(
        budgets=config.budgets,
        tracer=tracer,
        gateway=gateway,
        reasoning_model=config.models.reasoning,
        prompt_model=config.models.prompt,
        verifier_model=config.models.verifier,
        notion=notion,
        notifier=notifier,
        memory=memory,
        project_context=project_context,
        workspace_root=settings.workspace_root,
        coder=coder,
        sandbox=SandboxRunner(),
        github=github,
        base_branch="main",
        dry_run_pr=False,
        worktree_for=worktree_for,
        paused_index=paused_index,
        investigate_notify=investigate_notify,
        worktree_cleanup=worktree_cleanup,
        on_published=_pr_registrar(pr_index),
        on_terminal=_failure_recorder(failure_index),
    )


def _open_pr_index(workspace_root: Path) -> Any:
    from stromboli.orchestration.pr_index import PRIndex

    return PRIndex(workspace_root / ".stromboli" / "prs.db")


def _open_failure_index(workspace_root: Path) -> Any:
    from stromboli.orchestration.failure_index import FailureIndex

    return FailureIndex(workspace_root / ".stromboli" / "failures.db")


def _failure_recorder(index: Any) -> Any:
    """Build the ``on_terminal`` seam that captures a run's verdict (§1)."""
    from datetime import UTC, datetime

    from stromboli.orchestration.failure_index import FailureRecord

    def record(state: StromboliState) -> None:
        v = state.verdict
        goal = state.spec.goal if state.spec is not None else state.raw_request
        criteria = (
            "\n".join(state.spec.acceptance_criteria) if state.spec is not None else ""
        )
        last_test = state.test_results[-1] if state.test_results else None
        evidence = (
            f"{last_test.summary}\n{last_test.raw}" if last_test is not None else ""
        )
        index.record(
            FailureRecord(
                task_id=state.task_id,
                ts=datetime.now(UTC).isoformat(),
                goal=goal,
                decision=v.decision if v is not None else "",
                outcome=state.status,
                task_type=v.task_type if v is not None else "",
                failure_mode=v.failure_mode if v is not None else "",
                reason=v.reason if v is not None else "",
                expected=v.expected if v is not None else "",
                observed=v.observed if v is not None else "",
                cause=v.cause if v is not None else "",
                fix=v.fix if v is not None else "",
                diff=state.code_diff or "",
                test_evidence=evidence,
                acceptance_criteria=criteria,
                resolved=state.status == "done",
            )
        )

    return record


def _pr_registrar(pr_index: Any) -> Any:
    """Build the ``on_published`` seam that records an opened PR to watch."""
    from datetime import UTC, datetime

    from stromboli.orchestration.pr_index import PRWatch

    def register(state: StromboliState, worktree: Any, pr_url: str, number: int) -> None:
        goal = state.spec.goal if state.spec is not None else state.raw_request
        pr_index.register(
            PRWatch(
                task_id=state.task_id,
                repo=worktree.repo.full_name,
                branch=worktree.branch,
                pr_number=number,
                pr_url=pr_url,
                goal=goal,
                session_id=state.session_id,
            ),
            now=datetime.now(UTC).isoformat(),
        )

    return register


def _terminal_tag(result: Any | None) -> str:
    """A best-effort terminal-status tag for the trace."""
    if result is None:
        return "failure"
    status = result.get("status") if isinstance(result, dict) else None
    return str(status or "unknown")


__all__ = ["GraphDeps", "build_graph", "run_task"]
