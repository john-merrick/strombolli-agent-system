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
from typing import Any

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

    watch = sub.add_parser(
        "watch",
        help="Run autonomously: poll Notion for Ready tasks and build them, "
        "flagging each new task to Telegram.",
    )
    watch.add_argument(
        "--interval", type=float, default=30.0, help="Seconds between polls."
    )
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

    if args.command == "watch":
        return _watch(args.interval)

    return 2  # pragma: no cover - argparse enforces a valid subcommand


def _watch_once(
    notion: Any,
    db_id: str,
    notifier: Any,
    seen: set[str],
    *,
    run: Any,
    now: Any,
) -> list[Any]:
    """One poll pass: dispatch each newly-seen Ready task, flag it to Telegram.

    Returns the tasks dispatched this pass. Factored out of the loop so it's
    unit-testable. The Notion status guard prevents re-dispatch across passes;
    ``seen`` guards against double-notifying a task that lingers as To-do.
    """
    dispatched: list[Any] = []
    for task in notion.query_ready_tasks(db_id):
        if task.page_id in seen:
            continue
        seen.add(task.page_id)
        notifier.notify(
            f"🆕 New task picked up: {task.name} ({task.page_id}) at {now()}"
        )
        run(task)
        dispatched.append(task)
    return dispatched


def _watch(interval: float) -> int:
    """Autonomous loop: poll Notion for Ready tasks and build them (the daemon)."""
    import logging as _logging
    import time
    from datetime import datetime

    from stromboli.graph import run_task
    from stromboli.integrations.notion import NotionTaskClient
    from stromboli.integrations.telegram import make_notifier
    from stromboli.settings import load_settings

    log = _logging.getLogger("stromboli.watch")
    settings = load_settings()
    notion = NotionTaskClient(settings.notion_token)
    notifier = make_notifier(settings.telegram_bot_token, settings.telegram_chat_id)
    seen: set[str] = set()

    notifier.notify("👀 Stromboli is watching the Notion queue.")
    log.info("Watching Notion db %s every %.0fs", settings.notion_task_db_id, interval)

    def _run(task: Any) -> None:
        run_task("", source="notion", task_id=task.page_id, settings=settings)

    while True:
        try:
            done = _watch_once(
                notion, settings.notion_task_db_id, notifier, seen,
                run=_run, now=lambda: datetime.now().isoformat(timespec="seconds"),
            )
            if done:
                log.info("Dispatched %d task(s).", len(done))
        except KeyboardInterrupt:  # pragma: no cover
            log.info("Watcher stopped.")
            return 0
        except Exception:  # noqa: BLE001 - a poll error must not kill the loop
            log.exception("Poll failed; retrying next interval.")
        time.sleep(interval)


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
