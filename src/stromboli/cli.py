"""``python -m stromboli.cli`` — a terminal overview of the build queue.

Reads the run ledger directly (no running server, no tunnel, no secrets beyond
the workspace path) and prints what's running, what's queued, what recently
finished, and the timing/outcome metrics — the at-a-glance answer to "what is
Stromboli doing right now?" from the worker box itself.

The ledger path is taken from ``STROMBOLI_LEDGER_PATH`` if set, else derived from
``WORKSPACE_ROOT`` (the same file the worker writes).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from stromboli.app import LEDGER_RELATIVE_PATH
from stromboli.ledger import RunLedger, metrics_snapshot, status_snapshot


def ledger_path(env: dict[str, str] | None = None) -> Path:
    """Resolve the ledger file from the environment."""
    environ = env if env is not None else dict(os.environ)
    explicit = environ.get("STROMBOLI_LEDGER_PATH")
    if explicit:
        return Path(explicit)
    workspace = environ.get("WORKSPACE_ROOT")
    if not workspace:
        raise SystemExit(
            "Set STROMBOLI_LEDGER_PATH or WORKSPACE_ROOT to locate the run ledger."
        )
    return Path(workspace) / LEDGER_RELATIVE_PATH


def _line(record: dict[str, Any]) -> str:
    name = record.get("task_name") or record.get("page_id", "?")
    stage = record.get("stage") or record.get("state", "")
    return f"  {name}  —  {stage}"


def _secs(agg: dict[str, Any]) -> str:
    avg, mx = agg.get("avg_seconds"), agg.get("max_seconds")
    return "n/a" if avg is None else f"avg {avg}s  max {mx}s"


def render(status: dict[str, Any], metrics: dict[str, Any]) -> str:
    """Render the queue snapshot + metrics as a readable text report (pure)."""
    lines: list[str] = ["Stromboli — build queue", ""]

    running = status.get("running")
    lines.append(f"RUNNING: {_line(running).strip() if running else '(idle)'}")

    queued = status.get("queued") or []
    lines.append("")
    lines.append(f"QUEUED ({len(queued)}):" if queued else "QUEUED: (none)")
    lines += [_line(r) for r in queued]

    recent = status.get("recent") or []
    if recent:
        lines += ["", "RECENT:"]
        lines += [_line(r) for r in recent]

    builds = metrics.get("build_seconds", {})
    waits = metrics.get("queue_wait_seconds", {})
    lines += [
        "",
        f"METRICS (last {metrics.get('sample_size', 0)} finished):",
        f"  outcomes:   {metrics.get('outcomes', {})}",
        f"  build time: {_secs(builds)}",
        f"  queue wait: {_secs(waits)}",
    ]
    return "\n".join(lines)


def main() -> None:
    ledger = RunLedger(ledger_path())
    print(render(status_snapshot(ledger), metrics_snapshot(ledger)))


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    main()
