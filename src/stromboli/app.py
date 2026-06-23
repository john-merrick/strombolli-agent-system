"""Production assembly: wire settings → collaborators → FastAPI app.

This is the one place that constructs the *real* dependency graph from
:class:`~stromboli.settings.Settings` and hands it to the testable factories:

* a :class:`~stromboli.notion.NotionTaskClient` and
  :class:`~stromboli.pr.GitHubClient` for I/O;
* a :class:`~stromboli.worktree.WorktreeManager` rooted at ``WORKSPACE_ROOT``;
* a Langfuse :class:`~stromboli.observability.BuildTracer` (or the no-op when
  unset); composed into the end-to-end :func:`~stromboli.pipeline.run_build`;
* a :class:`~stromboli.worker.Worker` (the serial dispatch guard) whose build
  entrypoint is that pipeline; and
* the :func:`~stromboli.api.create_app` FastAPI surface, with the worker's
  ``dispatch`` wired as the background ``process_task``.

Keeping construction here (and out of the modules) is what let every story be
unit-tested with injected fakes.
"""

from __future__ import annotations

import logging
from functools import partial

from fastapi import FastAPI

from stromboli.api import create_app
from stromboli.breaker import BreakerConfig
from stromboli.notion import NotionTaskClient
from stromboli.observability import build_tracer
from stromboli.pipeline import BuildDeps, run_build
from stromboli.pr import GitHubClient
from stromboli.settings import Settings, load_settings
from stromboli.worker import Worker
from stromboli.worktree import WorktreeManager

logger = logging.getLogger(__name__)

#: Default per-task ceilings; override via the environment when tuning.
DEFAULT_MAX_ITERATIONS = 25
DEFAULT_MAX_COST_USD = 10.0


def build_deps(settings: Settings) -> BuildDeps:
    """Construct the real :class:`BuildDeps` from settings."""
    notion = NotionTaskClient(settings.notion_token)
    github = GitHubClient(settings.github_token)
    worktrees = WorktreeManager(settings.workspace_root, token=settings.github_token)
    tracer = build_tracer(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
    return BuildDeps(
        notion=notion,
        github=github,
        worktrees=worktrees,
        breaker_config=BreakerConfig(
            max_iterations=DEFAULT_MAX_ITERATIONS,
            max_cost_usd=DEFAULT_MAX_COST_USD,
        ),
        tracer=tracer,
    )


def create_stromboli_app(settings: Settings | None = None) -> FastAPI:
    """Build the fully-wired FastAPI application.

    Loads settings (failing fast on any missing env var) unless supplied, builds
    the real collaborator graph, and wires the serial worker's ``dispatch`` as
    the dispatch endpoint's background task.
    """
    settings = settings or load_settings()
    deps = build_deps(settings)
    worker = Worker(deps.notion, build=partial(run_build, deps=deps))

    def process_task(page_id: str) -> None:
        # The endpoint's background task ignores the return value; the serial
        # worker's DispatchOutcome is logged inside ``dispatch``.
        worker.dispatch(page_id)

    return create_app(
        dispatch_secret=settings.dispatch_shared_secret,
        process_task=process_task,
    )


__all__ = [
    "DEFAULT_MAX_COST_USD",
    "DEFAULT_MAX_ITERATIONS",
    "build_deps",
    "create_stromboli_app",
]
