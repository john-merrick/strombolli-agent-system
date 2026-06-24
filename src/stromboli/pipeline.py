"""The end-to-end build pipeline — composes the per-story pieces.

This is the integration layer the worker injects as its build entrypoint. Given
a claimed :class:`~stromboli.notion.Task` it runs the full flow described in the
README:

1. resolve the target repo and prepare an isolated worktree (US-005);
2. compile the task Spec into the worktree's ``scripts/prd.json`` (US-006);
3. drive the Ralph loop under a circuit breaker (US-007 / US-008);
4. on a breaker trip → route to ``Review`` with a note; otherwise open a PR and
   route Review/Complete per the autonomy table (US-010 / US-011);
5. append a resilient feedback summary (US-012); and
6. record the whole build as a Langfuse trace (US-013).

Every collaborator is injected via :class:`BuildDeps` so the orchestration is
unit-testable end-to-end with fakes — no git, network, or real ``claude``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from stromboli.breaker import BreakerConfig, CircuitBreaker
from stromboli.engine.gate import DEFAULT_COMMANDS, GateCommand, ObjectiveGate
from stromboli.engine.integrate import finalize_build, integrate_build
from stromboli.engine.orchestrator import GraphEngine
from stromboli.engine.policy import DeterministicPolicy
from stromboli.engine.result import BuildResult
from stromboli.loop import CCRunner, LoopResult, RalphLoop, default_cc_runner
from stromboli.notion import Repo, Task
from stromboli.observability import BuildTracer, NullTracer
from stromboli.pr import GitRunner
from stromboli.prd import build_prd, write_prd
from stromboli.worktree import Worktree

logger = logging.getLogger(__name__)

#: The two selectable build engines (see ``STROMBOLI_ENGINE``).
ENGINE_RALPH = "ralph"
ENGINE_GRAPH = "graph"


class LoopRunner(Protocol):
    """The slice of :class:`~stromboli.loop.RalphLoop` the pipeline drives."""

    def run(self, worktree_root: str | Path) -> LoopResult: ...


class GraphRunner(Protocol):
    """The slice of :class:`~stromboli.engine.orchestrator.GraphEngine` driven."""

    def run(
        self, worktree_root: str | Path, *, spec: str, resume: bool = ...
    ) -> BuildResult: ...


class WorktreeProvider(Protocol):
    """Yields a prepared, auto-cleaned worktree (a :class:`WorktreeManager`)."""

    def worktree(
        self, repo: Repo, task_id: str, task_name: str
    ) -> AbstractContextManager[Worktree]: ...


def _default_make_loop(breaker: CircuitBreaker) -> LoopRunner:
    """Default loop factory: a real :class:`RalphLoop` under the breaker."""
    return RalphLoop(breaker=breaker)


def _default_make_graph(deps: BuildDeps) -> GraphRunner:
    """Default graph factory: a real :class:`GraphEngine` from the deps."""
    return GraphEngine(
        policy=DeterministicPolicy(),
        gate=ObjectiveGate(deps.gate_commands or DEFAULT_COMMANDS),
        cc_run=deps.graph_cc_run or default_cc_runner,
        breaker_config=deps.breaker_config,
    )


@dataclass
class BuildDeps:
    """The collaborators :func:`run_build` needs, all injectable for tests."""

    notion: Any
    github: Any
    worktrees: WorktreeProvider
    breaker_config: BreakerConfig
    tracer: BuildTracer = field(default_factory=NullTracer)
    make_loop: Callable[[CircuitBreaker], LoopRunner] = _default_make_loop
    #: The git seam passed to PR mechanics; ``None`` uses the real git runner.
    git_run: GitRunner | None = None
    #: Which engine to run — ``ralph`` (default) or ``graph``.
    engine: str = ENGINE_RALPH
    #: Graph-engine factory (injected for tests).
    make_graph: Callable[[BuildDeps], GraphRunner] = _default_make_graph
    #: Per-repo objective-gate command override; ``None`` uses the defaults.
    gate_commands: Sequence[GateCommand] | None = None
    #: The CC runner for graph agents; ``None`` uses the real ``claude`` runner.
    graph_cc_run: CCRunner | None = None


def run_build(task: Task, deps: BuildDeps) -> None:
    """Run the full build for a claimed task. Best-effort, never re-raises.

    The configured engine (Ralph or graph) produces a
    :class:`~stromboli.engine.result.BuildResult` that the *same* integrator /
    finalize path consumes. The worktree is always torn down (its context
    manager cleans up on success or failure); a failure mid-build is recorded on
    the trace and surfaced in the feedback summary rather than crashing the
    worker.
    """
    repo = deps.notion.get_project_repo(task)
    error: str | None = None
    result: BuildResult | None = None
    pr_url: str | None = None

    with deps.worktrees.worktree(repo, task.page_id, task.name) as worktree:
        try:
            if deps.engine == ENGINE_GRAPH:
                result = _run_graph(task, worktree, deps)
            else:
                result = _run_ralph(task, worktree, deps)

            pr_url = integrate_build(
                task=task,
                worktree=worktree,
                result=result,
                notion=deps.notion,
                github=deps.github,
                git_run=deps.git_run,
            )
        except Exception as exc:  # noqa: BLE001 - one task must not crash the worker
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("Build failed for task %s", task.page_id)

        finalize_build(
            task=task,
            result=result,
            pr_url=pr_url,
            error=error,
            notion=deps.notion,
            tracer=deps.tracer,
        )


def _run_ralph(task: Task, worktree: Worktree, deps: BuildDeps) -> LoopResult:
    """Compile the PRD and drive the Ralph loop under the breaker."""
    prd = build_prd(
        task,
        branch=worktree.branch,
        max_attempts=deps.breaker_config.max_iterations,
    )
    write_prd(worktree.path, prd)

    breaker = CircuitBreaker(deps.breaker_config)
    loop = deps.make_loop(breaker)
    return loop.run(worktree.path)


def _run_graph(task: Task, worktree: Worktree, deps: BuildDeps) -> BuildResult:
    """Drive the graph engine over the task spec in the worktree."""
    engine = deps.make_graph(deps)
    return engine.run(worktree.path, spec=task.spec)


__all__ = [
    "ENGINE_GRAPH",
    "ENGINE_RALPH",
    "BuildDeps",
    "GraphRunner",
    "LoopRunner",
    "WorktreeProvider",
    "run_build",
]
