"""Stromboli CLI: ``python -m stromboli run --task "<text>"``.

The runtime is a per-task graph invocation, not a server (PRD §10 Phase 0 DoD).
``run`` builds the initial state from the CLI args and drives one task through
the compiled LangGraph, printing the terminal status. Logging goes to stderr;
set ``STROMBOLI_LOG_FILE`` to also persist it (with tracebacks) to a file.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _build_log_handlers(log_file: str | None) -> list[logging.Handler]:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    return handlers


def configure_logging() -> None:
    """Configure root logging from ``STROMBOLI_LOG_LEVEL`` / ``STROMBOLI_LOG_FILE``."""
    logging.basicConfig(
        level=os.environ.get("STROMBOLI_LOG_LEVEL", "INFO"),
        format=LOG_FORMAT,
        handlers=_build_log_handlers(os.environ.get("STROMBOLI_LOG_FILE")),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stromboli")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run one task through the graph.")
    run.add_argument(
        "--task",
        default="",
        help="The raw task request text (required for --source cli; for "
        "--source notion it is hydrated from the task page).",
    )
    run.add_argument("--task-id", default=None, help="Notion page id / run id.")
    run.add_argument(
        "--source",
        default="cli",
        choices=["cli", "notion", "telegram"],
        help="The intake source (default: cli).",
    )

    poll = sub.add_parser(
        "poll", help="Run every Ready Notion task through the graph (the front-end)."
    )
    poll.add_argument(
        "--once", action="store_true", help="Drain the ready queue once and exit."
    )

    dash = sub.add_parser("dashboard", help="Serve the live watchtower dashboard.")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)

    if args.command == "run":
        if args.source == "cli" and not args.task:
            print("error: --task is required for --source cli", file=sys.stderr)
            return 2
        if args.source == "notion" and not args.task_id:
            print("error: --task-id is required for --source notion", file=sys.stderr)
            return 2
        # Imported lazily so `--help` doesn't pull in LangGraph / settings.
        from stromboli.graph import run_task

        final = run_task(
            args.task, source=args.source, task_id=args.task_id
        )
        print(f"task {final.task_id}: {final.status}")
        if final.pr_url:
            print(f"PR: {final.pr_url}")
        return 0

    if args.command == "poll":
        return _poll()

    if args.command == "dashboard":
        return _dashboard(args.host, args.port)

    return 2  # pragma: no cover - argparse enforces a valid subcommand


def _dashboard(host: str, port: int) -> int:
    """Serve the watchtower over the workspace's runs registry.

    Read-only observability — it needs **only** ``WORKSPACE_ROOT`` (the registry
    location), not any secrets, so it runs without 1Password / ``op``.
    """
    import uvicorn

    from stromboli.dashboard.app import create_dashboard
    from stromboli.observability.runs import RunsRegistry

    raw = os.environ.get("WORKSPACE_ROOT")
    if not raw:
        print("error: set WORKSPACE_ROOT (the dashboard reads its runs registry "
              "from <WORKSPACE_ROOT>/.stromboli/runs.db)", file=sys.stderr)
        return 2
    workspace = Path(raw).expanduser()
    registry = RunsRegistry(workspace / ".stromboli" / "runs.db")
    print(f"Stromboli watchtower → http://{host}:{port}  (workspace: {workspace})")
    uvicorn.run(create_dashboard(registry), host=host, port=port)
    return 0


def _poll() -> int:
    """Drain the Ready Notion tasks through the graph (Notion is the front-end)."""
    from stromboli.graph import run_task
    from stromboli.integrations.notion import NotionTaskClient
    from stromboli.settings import load_settings

    settings = load_settings()
    notion = NotionTaskClient(settings.notion_token)
    tasks = notion.query_ready_tasks(settings.notion_task_db_id)
    if not tasks:
        print("No Ready tasks.")
        return 0
    for task in tasks:
        final = run_task("", source="notion", task_id=task.page_id, settings=settings)
        print(f"task {final.task_id} ({task.name}): {final.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
