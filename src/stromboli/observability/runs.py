"""The runs registry — the self-contained source of truth for the dashboard.

A small SQLite store every run writes its live state to (status, current node,
per-node timing, model, tokens, cost, pid) and that the watchtower dashboard
reads. It is also the **control plane**: the dashboard sets a cancel flag the
run checks cooperatively between nodes (and can hard-kill the pid).

Multi-process safe: the run process and the dashboard process are different
processes, so each call opens a short-lived WAL connection rather than holding
one. All writes are best-effort — observability must never crash a run.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT, task_name TEXT, source TEXT,
    status TEXT, current_node TEXT, pid INTEGER,
    started_at REAL, ended_at REAL,
    pr_url TEXT, total_tokens INTEGER DEFAULT 0, total_cost_usd REAL DEFAULT 0.0,
    error TEXT, cancel_requested INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS node_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, node TEXT, phase TEXT, ts REAL,
    model TEXT, output_tokens INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, idx_ INTEGER, tools TEXT, output_tokens INTEGER, ts REAL
);
"""


def _now() -> float:
    return time.time()


class RunsRegistry:
    """SQLite-backed registry of runs, node events, and turns (+ cancel flag)."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        try:
            with self._conn() as c:
                c.execute(sql, params)
        except Exception as exc:  # noqa: BLE001 - registry must never crash a run
            logger.warning("RunsRegistry write failed: %s", exc)

    # -- writes (the run process) ------------------------------------------ #
    def register_run(
        self, run_id: str, *, task_id: str, task_name: str, source: str, pid: int
    ) -> None:
        self._exec(
            "INSERT OR REPLACE INTO runs("
            "run_id, task_id, task_name, source, status, current_node, pid, "
            "started_at, cancel_requested) VALUES (?,?,?,?,?,?,?,?,0)",
            (run_id, task_id, task_name, source, "running", "intake", pid, _now()),
        )

    def start_node(self, run_id: str, node: str) -> None:
        self._exec("UPDATE runs SET current_node=? WHERE run_id=?", (node, run_id))
        self._exec(
            "INSERT INTO node_events(run_id, node, phase, ts) VALUES (?,?,?,?)",
            (run_id, node, "start", _now()),
        )

    def end_node(
        self,
        run_id: str,
        node: str,
        *,
        detail: str = "",
        model: str | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self._exec(
            "INSERT INTO node_events(run_id, node, phase, ts, model, output_tokens, "
            "detail) VALUES (?,?,?,?,?,?,?)",
            (run_id, node, "end", _now(), model, output_tokens, detail[:2000]),
        )
        if output_tokens:
            self._exec(
                "UPDATE runs SET total_tokens=total_tokens+? WHERE run_id=?",
                (output_tokens, run_id),
            )

    def record_turn(
        self, run_id: str, idx: int, tools: list[str], output_tokens: int | None
    ) -> None:
        self._exec(
            "INSERT INTO turns(run_id, idx_, tools, output_tokens, ts) "
            "VALUES (?,?,?,?,?)",
            (run_id, idx, ",".join(tools), output_tokens, _now()),
        )

    def finish_run(
        self, run_id: str, *, status: str, pr_url: str | None = None,
        error: str | None = None,
    ) -> None:
        self._exec(
            "UPDATE runs SET status=?, ended_at=?, pr_url=?, error=?, current_node=NULL "
            "WHERE run_id=?",
            (status, _now(), pr_url, error, run_id),
        )

    # -- control (the dashboard) ------------------------------------------- #
    def request_cancel(self, run_id: str) -> None:
        self._exec("UPDATE runs SET cancel_requested=1 WHERE run_id=?", (run_id,))

    def is_cancel_requested(self, run_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT cancel_requested FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    # -- reads (the dashboard) --------------------------------------------- #
    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            run = dict(row)
            run["node_events"] = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM node_events WHERE run_id=? ORDER BY id", (run_id,)
                ).fetchall()
            ]
            run["turns"] = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM turns WHERE run_id=? ORDER BY id", (run_id,)
                ).fetchall()
            ]
        return run

    def summary(self) -> dict[str, Any]:
        """Roll-up for the reporting view: counts, cost, durations by status."""
        with self._conn() as c:
            by_status = {
                r["status"]: r["n"]
                for r in c.execute(
                    "SELECT status, COUNT(*) n FROM runs GROUP BY status"
                ).fetchall()
            }
            agg = c.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tokens, "
                "COALESCE(SUM(total_cost_usd),0) cost FROM runs"
            ).fetchone()
        return {
            "total_runs": agg["n"],
            "by_status": by_status,
            "total_tokens": agg["tokens"],
            "total_cost_usd": agg["cost"],
        }


__all__ = ["RunsRegistry"]
