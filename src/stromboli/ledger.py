"""The run ledger — a persistent record of every dispatch's lifecycle.

Ticking *Ready* in Notion used to fire a build straight at a serial worker that
**dropped** any dispatch arriving while a build was in flight, with no record of
it ever happening. The ledger fixes that: every dispatch becomes a durable row
that moves through a lifecycle —

    queued → running (stage, heartbeat) → done | failed | skipped

— so nothing is silently lost, a restart can resume the queue, and a single
SQLite file answers "what's running, what's waiting, what just finished, and
where is the time going". It is the spine the queue consumer, the status
endpoint, and (later) notifications all read from.

Backed by stdlib :mod:`sqlite3` (no new dependency). The clock is injected so the
lifecycle timestamps are deterministic under test.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

#: Default number of finished runs the ``recent`` view returns.
DEFAULT_RECENT_LIMIT: Final = 20

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id      TEXT NOT NULL,
    task_name    TEXT,
    engine       TEXT,
    state        TEXT NOT NULL,
    stage        TEXT,
    outcome      TEXT,
    error        TEXT,
    queued_at    TEXT NOT NULL,
    started_at   TEXT,
    ended_at     TEXT,
    heartbeat_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs (state, id);
"""


class RunState(StrEnum):
    """Where a dispatch sits in its lifecycle."""

    #: Accepted and waiting for the consumer.
    QUEUED = "queued"
    #: The consumer is building it now.
    RUNNING = "running"
    #: The build ran to completion.
    DONE = "done"
    #: The build raised — see ``error``.
    FAILED = "failed"
    #: The guard declined it (not Ready / not Agent / already claimed).
    SKIPPED = "skipped"


#: The terminal states a run can finish in.
TERMINAL_STATES: Final = frozenset(
    {RunState.DONE, RunState.FAILED, RunState.SKIPPED}
)


@dataclass(frozen=True)
class RunRecord:
    """One dispatch's row, as read back from the ledger."""

    id: int
    page_id: str
    task_name: str | None
    engine: str | None
    state: RunState
    stage: str | None
    outcome: str | None
    error: str | None
    queued_at: str
    started_at: str | None
    ended_at: str | None
    heartbeat_at: str | None

    @property
    def is_terminal(self) -> bool:
        """Whether this run has finished (done / failed / skipped)."""
        return self.state in TERMINAL_STATES


def _row_to_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        page_id=row["page_id"],
        task_name=row["task_name"],
        engine=row["engine"],
        state=RunState(row["state"]),
        stage=row["stage"],
        outcome=row["outcome"],
        error=row["error"],
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        heartbeat_at=row["heartbeat_at"],
    )


class RunLedger:
    """A SQLite-backed lifecycle ledger for dispatches. Thread-safe."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = str(path)
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        # Guards the claim_next read-then-update so two consumers never grab the
        # same row; also serialises writers on the single connection-per-call.
        self._lock = threading.Lock()
        self._init_db()

    # -- lifecycle -------------------------------------------------------- #
    def enqueue(
        self, page_id: str, *, task_name: str | None = None, engine: str | None = None
    ) -> RunRecord:
        """Record a new dispatch as ``queued`` and return its row."""
        now = self._now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (page_id, task_name, engine, state, queued_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (page_id, task_name, engine, RunState.QUEUED.value, now),
            )
            run_id = int(cur.lastrowid or 0)
        return self.get(run_id)

    def claim_next(self) -> RunRecord | None:
        """Atomically take the oldest ``queued`` run and mark it ``running``."""
        now = self._now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM runs WHERE state = ? ORDER BY id LIMIT 1",
                (RunState.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE runs SET state = ?, started_at = ?, heartbeat_at = ? "
                "WHERE id = ?",
                (RunState.RUNNING.value, now, now, row["id"]),
            )
            run_id = int(row["id"])
        return self.get(run_id)

    def set_stage(self, run_id: int, stage: str) -> None:
        """Update the running run's current stage and bump its heartbeat."""
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE runs SET stage = ?, heartbeat_at = ? WHERE id = ?",
                (stage, now, run_id),
            )

    def heartbeat(self, run_id: int) -> None:
        """Bump the running run's heartbeat (liveness, no stage change)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE id = ?",
                (self._now(), run_id),
            )

    def finish(
        self,
        run_id: int,
        state: RunState,
        *,
        outcome: str | None = None,
        error: str | None = None,
    ) -> None:
        """Move a run to a terminal state, stamping ``ended_at``."""
        if state not in TERMINAL_STATES:
            raise ValueError(f"{state} is not a terminal state")
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE runs SET state = ?, outcome = ?, error = ?, ended_at = ? "
                "WHERE id = ?",
                (state.value, outcome, error, self._now(), run_id),
            )

    # -- views ------------------------------------------------------------ #
    def get(self, run_id: int) -> RunRecord:
        """Read one run by id (raises :class:`KeyError` if absent)."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"no run with id {run_id}")
        return _row_to_record(row)

    def running(self) -> RunRecord | None:
        """The run currently building, if any."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE state = ? ORDER BY id LIMIT 1",
                (RunState.RUNNING.value,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def queued(self) -> list[RunRecord]:
        """The waiting runs, oldest first (FIFO order)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE state = ? ORDER BY id",
                (RunState.QUEUED.value,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def recent(self, limit: int = DEFAULT_RECENT_LIMIT) -> list[RunRecord]:
        """The most recently finished runs, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE ended_at IS NOT NULL "
                "ORDER BY ended_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def position(self, run_id: int) -> int:
        """How many queued runs are ahead of ``run_id`` (0 = next up)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS ahead FROM runs WHERE state = ? AND id < ?",
                (RunState.QUEUED.value, run_id),
            ).fetchone()
        return int(row["ahead"])

    # -- internals -------------------------------------------------------- #
    def _now(self) -> str:
        return self._clock().isoformat()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)


__all__ = [
    "DEFAULT_RECENT_LIMIT",
    "TERMINAL_STATES",
    "RunLedger",
    "RunRecord",
    "RunState",
]
