"""The integrator adapter: one finalize path for both engines (Phase 3).

A finished build — Ralph's or the graph's — ends the same way: trip → Review,
else open a PR and route Review/Complete, then append a resilient feedback
summary and record the Langfuse trace. This module is that path, expressed once
over the engine-agnostic :class:`~stromboli.engine.result.BuildResult`, reusing
the already-covered ``publish_pr`` / ``route_task`` / ``handle_trip`` /
``resilient_append`` / ``record_build_trace`` building blocks.

Collaborators are passed individually (not as a ``BuildDeps``) so this stays
lower-level than ``pipeline`` — both ``pipeline.run_build`` (Ralph) and the
graph wiring delegate here, keeping a single integration code path.
"""

from __future__ import annotations

from typing import Any

from stromboli.breaker import handle_trip
from stromboli.engine.result import BuildResult
from stromboli.notion import Task
from stromboli.observability import BuildTracer, record_build_trace
from stromboli.pr import GitRunner, publish_pr
from stromboli.routing import route_task
from stromboli.worktree import Worktree
from stromboli.writeback import build_feedback_summary, resilient_append


def integrate_build(
    *,
    task: Task,
    worktree: Worktree,
    result: BuildResult,
    notion: Any,
    github: Any,
    git_run: GitRunner | None = None,
) -> str | None:
    """Route the finished build: trip → Review; else open a PR and route.

    Returns the opened PR URL, or ``None`` when the breaker tripped or the diff
    was empty.
    """
    if result.tripped and result.trip is not None:
        handle_trip(notion, task.page_id, result.trip)
        return None

    publish = publish_pr(notion, github, worktree, task, run=git_run)
    route_task(notion, task, publish)
    return publish.pr_url


def finalize_build(
    *,
    task: Task,
    result: BuildResult | None,
    pr_url: str | None,
    error: str | None,
    notion: Any,
    tracer: BuildTracer,
) -> None:
    """Append the resilient feedback summary and record the trace.

    Best-effort and engine-agnostic: ``result`` may be ``None`` when the build
    failed before producing one.
    """
    blocked = result.blocked_items() if result is not None else []
    completed = result.completed_count() if result is not None else 0
    trip = result.trip if result is not None else None

    summary = build_feedback_summary(
        pr_url=pr_url,
        blocked_items=blocked,
        completed_count=completed,
        trip=trip,
    )
    if error is not None:
        summary += f"\n\n> ⚠️ Build error: {error}"
    resilient_append(notion, task.page_id, summary)

    if result is not None:
        record_build_trace(tracer, task, result, error=error)


__all__ = [
    "finalize_build",
    "integrate_build",
]
