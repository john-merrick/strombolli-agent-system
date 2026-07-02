"""Per-run trace file — a local, always-on record of what each node did.

Langfuse (PRD §8) is the rich backend, but it needs working credentials and a
network round-trip; this is the **local, dependency-free** companion so you can
always see *where a run broke down*: one line per node with its key outputs
(status, generated prompt, diff size, test result, verdict, escalation reason),
written under ``<workspace>/.stromboli/runs/<task_id>/``.

Both a machine-readable ``trace.jsonl`` and a human-readable ``trace.md`` are
written. The base :class:`RunTrace` is a no-op (used in unit tests and when no
workspace is configured).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Per-field truncation so a big diff / log doesn't bloat the trace.
_MAX_FIELD = 4000


class RunTrace:
    """No-op trace recorder. Subclass writes to disk."""

    def record(self, node: str, output: dict[str, Any]) -> None:
        """Record a node's partial-state output."""

    def event(self, name: str, detail: str) -> None:
        """Record a free-form event (e.g. an error)."""


def summarize_output(output: dict[str, Any]) -> dict[str, Any]:
    """Reduce a node's partial-state dict to a compact, readable summary.

    Shared by the local run trace and the Langfuse node spans (the span's
    ``output`` pane) — in LangGraph a node's returned partial state *is* the
    state diff, so this is exactly "what this node changed".
    """
    summary: dict[str, Any] = {}
    for key, value in output.items():
        if key == "code_diff" and isinstance(value, str):
            summary["code_diff_chars"] = len(value)
            summary["code_diff_head"] = value[:400]
        elif key == "test_results" and isinstance(value, list):
            summary["test_results"] = [
                {"passed": getattr(t, "passed", None),
                 "summary": getattr(t, "summary", "")[:200]}
                for t in value
            ]
        elif key in ("spec", "verdict") and value is not None:
            dump = getattr(value, "model_dump", None)
            summary[key] = dump() if callable(dump) else str(value)[:_MAX_FIELD]
        elif isinstance(value, str):
            summary[key] = value[:_MAX_FIELD]
        else:
            summary[key] = value
    return summary


class FileRunTrace(RunTrace):
    """Writes a ``trace.jsonl`` + ``trace.md`` under the run directory."""

    def __init__(self, workspace_root: str | Path, task_id: str) -> None:
        self._dir = Path(workspace_root) / ".stromboli" / "runs" / task_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._jsonl = self._dir / "trace.jsonl"
        self._md = self._dir / "trace.md"
        self._n = 0

    @property
    def directory(self) -> Path:
        return self._dir

    def _append(self, record: dict[str, Any], heading: str, body: str) -> None:
        try:
            with self._jsonl.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            with self._md.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## {heading}\n\n{body}\n")
        except Exception as exc:  # noqa: BLE001 - tracing must never crash a run
            logger.warning("RunTrace write failed: %s", exc)

    def record(self, node: str, output: dict[str, Any]) -> None:
        self._n += 1
        summary = summarize_output(output)
        self._append(
            {"step": self._n, "node": node, "output": summary},
            f"{self._n}. {node}",
            "```json\n" + json.dumps(summary, indent=2) + "\n```",
        )

    def event(self, name: str, detail: str) -> None:
        self._n += 1
        self._append(
            {"step": self._n, "event": name, "detail": detail[:_MAX_FIELD]},
            f"{self._n}. event: {name}",
            detail[:_MAX_FIELD],
        )


__all__ = ["FileRunTrace", "RunTrace", "summarize_output"]
