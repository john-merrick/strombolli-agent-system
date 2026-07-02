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
    run.add_argument(
        "--repo",
        default=None,
        help="For --source cli: the target repository the task is built in — "
        "a local path or a GitHub 'owner/name' (required; a clone-per-task "
        "worktree is provisioned from it).",
    )
    run.add_argument(
        "--dry-run-pr",
        action="store_true",
        help="Log the PR intent instead of pushing and opening a real PR.",
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

    sub.add_parser(
        "investigate-serve",
        help="Run the investigate loop: long-poll the investigate-bot to resolve "
        "and resume suspended (Queued) tasks via chat.",
    )

    optv = sub.add_parser(
        "optimize-verifier",
        help="Export the labelled failure dataset and score the current verifier "
        "prompt against it (GEPA scaffold; self-improving §2).",
    )
    optv.add_argument(
        "--export-only", action="store_true",
        help="Only export failures.db → the labelled dataset JSON; skip scoring.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)

    if args.command == "run":
        if args.source == "cli" and not args.task:
            print("error: --task is required for --source cli", file=sys.stderr)
            return 2
        if args.source == "cli" and not args.repo:
            # Fail fast: without a repo the coding node has no worktree to
            # build in (only Notion tasks resolve one from their Project).
            print("error: --repo is required for --source cli", file=sys.stderr)
            return 2
        if args.source == "notion" and not args.task_id:
            print("error: --task-id is required for --source notion", file=sys.stderr)
            return 2
        # Imported lazily so `--help` doesn't pull in LangGraph / settings.
        from stromboli.graph import run_task

        final = run_task(
            args.task,
            source=args.source,
            task_id=args.task_id,
            repo=args.repo,
            dry_run_pr=True if args.dry_run_pr else None,
        )
        print(f"task {final.task_id}: {final.status}")
        if final.pr_url:
            print(f"PR: {final.pr_url}")
        return 0

    if args.command == "poll":
        return _poll()

    if args.command == "watch":
        return _watch(args.interval)

    if args.command == "investigate-serve":
        return _investigate_serve()

    if args.command == "optimize-verifier":
        return _optimize_verifier(export_only=args.export_only)

    return 2  # pragma: no cover - argparse enforces a valid subcommand


def _investigate_serve() -> int:
    """Run the investigate loop service (the bidirectional escalation resolver)."""
    from stromboli.orchestration.investigate import serve_from_settings
    from stromboli.settings import load_settings

    serve_from_settings(load_settings())
    return 0


def _optimize_verifier(*, export_only: bool) -> int:
    """Export the labelled failure dataset and score the current verifier prompt.

    The GEPA scaffold (self-improving §2): builds the trainset from human-
    labelled failures and reports the current judge's agreement. Candidate
    generation (DSPy) needs the ``optimize`` extra and enough labelled volume;
    adoption is always a human decision, never automatic.
    """
    from stromboli.graph import _open_failure_index
    from stromboli.settings import load_settings

    settings = load_settings()
    index = _open_failure_index(settings.workspace_root)
    out = settings.workspace_root / ".stromboli" / "verifier_labelled.json"
    n = index.export_verifier_dataset(out)
    print(f"Exported {n} labelled case(s) → {out}")
    if export_only:
        return 0
    if n == 0:
        print("No labelled cases yet — label verifier decisions first "
              "(accept/reject) to build the trainset.")
        return 0
    from stromboli.llm.gateway import build_gateway
    from stromboli.nodes.verifier import DEFAULT_VERIFIER_SYSTEM
    from stromboli.observability.evals.verifier_optimize import evaluate_prompt

    gateway = build_gateway(
        base_url=settings.litellm_base_url, api_key=settings.litellm_api_key
    )
    score = evaluate_prompt(
        gateway, settings.verifier_model, DEFAULT_VERIFIER_SYSTEM, dataset_path=out
    )
    print(f"Current verifier prompt agreement on labelled set: {score:.2f}")
    print("To optimize: install the 'optimize' extra and run GEPA over this set; "
          "adopt a candidate only if it strictly beats this score.")
    return 0


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

    ``seen`` is pruned to the tasks the query still returns: once a task
    leaves the Ready queue (built / escalated) it is *forgotten*, so a task the
    human fixes and re-ticks Ready is picked up again — the whole autonomous
    loop, not just a task's first appearance. (A long-lived ``seen`` silently
    ignored every re-queued task until the watcher was restarted.)
    """
    dispatched: list[Any] = []
    ready = notion.query_ready_tasks(db_id)
    seen.intersection_update({t.page_id for t in ready})
    for task in ready:
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

    # The PR feedback loop shares this daemon: an opened PR is a checkpoint, so
    # every few passes we sweep Stromboli's open PRs and fix CI/comment feedback
    # (built lazily so a watcher without GitHub config still runs the Notion
    # drain). See docs/design-pr-feedback-loop.md.
    from stromboli.orchestration.pr_feedback import build_from_settings

    try:
        pr_feedback = build_from_settings(settings)
    except Exception:  # noqa: BLE001 - the Notion drain must still run
        log.exception("PR feedback loop unavailable; running Notion drain only.")
        pr_feedback = None

    pass_n = 0
    while True:
        try:
            done = _watch_once(
                notion, settings.notion_task_db_id, notifier, seen,
                run=_run, now=lambda: datetime.now().isoformat(timespec="seconds"),
            )
            if done:
                log.info("Dispatched %d task(s).", len(done))
            # Sweep PRs every 4th pass (~2 min at the default 30s interval) so a
            # burst of Notion work doesn't starve PR follow-up, and vice versa.
            if pr_feedback is not None and pass_n % 4 == 0:
                acted = pr_feedback.sweep()
                if acted:
                    log.info("PR feedback acted on %d PR(s): %s", len(acted), acted)
        except KeyboardInterrupt:  # pragma: no cover
            log.info("Watcher stopped.")
            return 0
        except Exception:  # noqa: BLE001 - a poll error must not kill the loop
            log.exception("Poll failed; retrying next interval.")
        pass_n += 1
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
