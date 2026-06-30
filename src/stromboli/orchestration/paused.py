"""The paused-task index — durable record of tasks suspended for the investigate loop.

A task that escalates for a *task-driven* reason (ambiguous spec, verifier
rejection, revision cap) is **suspended** rather than parked: its whole
:class:`~stromboli.state.StromboliState` is serialized here so the run can be
**resumed in place** once the human replies. The store also owns:

- the short ``#N`` handle the human uses to address a task in Telegram, and
- the conversation transcript (so a service restart keeps context), and
- the metadata the expiry sweeper needs (``paused_at``, ``worktree_path``).

This is the operational source of truth for "what is resumable / which ``#N``";
the graph state needed to actually resume lives in ``state_json``. It is *not*
the LangGraph checkpointer — the Prefect runtime drives plain ``state -> state``
phases, so persisting the state model is the resume mechanism (design DL-2).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from stromboli.state import StromboliState

#: Lifecycle of a paused row.
PausedState = str  # "open" | "resumed" | "expired"


@dataclass(frozen=True)
class PausedTask:
    """One suspended task awaiting human input via the investigate loop."""

    task_id: str
    ref: int  # the short #N handle
    reason: str
    paused_at: str  # ISO-8601
    state: PausedState
    worktree_path: str | None = None
    session_id: str | None = None
    chat_id: str | None = None
    #: The guidance the investigator proposed and the human has yet to apply.
    guidance: str | None = None
    #: Human-readable task name (for the opener / `/queued` listing).
    name: str = ""


class PausedIndex:
    """SQLite-backed index of suspended tasks (one row per ``task_id``)."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paused (
                    task_id       TEXT PRIMARY KEY,
                    ref           INTEGER NOT NULL,
                    reason        TEXT NOT NULL DEFAULT '',
                    paused_at     TEXT NOT NULL,
                    state         TEXT NOT NULL DEFAULT 'open',
                    worktree_path TEXT,
                    session_id    TEXT,
                    chat_id       TEXT,
                    name          TEXT NOT NULL DEFAULT '',
                    guidance      TEXT,
                    transcript    TEXT NOT NULL DEFAULT '[]',
                    state_json    TEXT NOT NULL
                )
                """
            )
            # Key/value side table — the persisted Telegram long-poll offset, so a
            # service restart neither reprocesses nor drops messages.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
            )

    # -- writes -------------------------------------------------------------- #
    def suspend(
        self,
        state: StromboliState,
        *,
        reason: str,
        name: str = "",
        worktree_path: str | None = None,
        paused_at: str | None = None,
    ) -> PausedTask:
        """Record a suspended task (assigning the next free ``#N``) and persist its state.

        Re-suspending an already-open task keeps its ``#N`` and refreshes the
        stored state/reason (idempotent on ``task_id``).
        """
        when = paused_at or datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT ref FROM paused WHERE task_id = ? AND state = 'open'",
                (state.task_id,),
            ).fetchone()
            ref = existing["ref"] if existing else self._next_ref(conn)
            conn.execute(
                """
                INSERT INTO paused
                    (task_id, ref, reason, paused_at, state,
                     worktree_path, session_id, name, state_json)
                VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    ref           = excluded.ref,
                    reason        = excluded.reason,
                    paused_at     = excluded.paused_at,
                    state         = 'open',
                    worktree_path = excluded.worktree_path,
                    session_id    = excluded.session_id,
                    name          = excluded.name,
                    state_json    = excluded.state_json
                """,
                (
                    state.task_id, ref, reason, when,
                    worktree_path, state.session_id, name, state.model_dump_json(),
                ),
            )
        return PausedTask(
            task_id=state.task_id, ref=ref, reason=reason, paused_at=when,
            state="open", worktree_path=worktree_path, session_id=state.session_id,
            name=name,
        )

    def resolve(self, task_id: str, *, state: PausedState) -> None:
        """Close a paused row (``resumed`` or ``expired``), freeing its ``#N``."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE paused SET state = ? WHERE task_id = ?", (state, task_id)
            )

    # -- conversation transcript + proposed guidance ----------------------- #
    def append_message(self, task_id: str, role: str, content: str) -> None:
        """Append one ``{role, content}`` turn to the task's stored transcript."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT transcript FROM paused WHERE task_id = ?", (task_id,)
            ).fetchone()
            history = json.loads(row["transcript"]) if row else []
            history.append({"role": role, "content": content})
            conn.execute(
                "UPDATE paused SET transcript = ? WHERE task_id = ?",
                (json.dumps(history), task_id),
            )

    def transcript(self, task_id: str) -> list[dict[str, str]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT transcript FROM paused WHERE task_id = ?", (task_id,)
            ).fetchone()
        return list(json.loads(row["transcript"])) if row else []

    def set_guidance(self, task_id: str, guidance: str) -> None:
        """Stash the investigator's proposed guidance, applied on the human's ✅."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE paused SET guidance = ? WHERE task_id = ?",
                (guidance, task_id),
            )

    # -- persisted Telegram long-poll offset ------------------------------- #
    def get_offset(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'offset'"
            ).fetchone()
        return int(row["value"]) if row else None

    def set_offset(self, offset: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('offset', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(offset),),
            )

    def _next_ref(self, conn: sqlite3.Connection) -> int:
        """Smallest free positive int among currently-open rows (#1, #2, …)."""
        taken = {
            row["ref"]
            for row in conn.execute("SELECT ref FROM paused WHERE state = 'open'")
        }
        ref = 1
        while ref in taken:
            ref += 1
        return ref

    # -- reads --------------------------------------------------------------- #
    def get(self, task_id: str) -> PausedTask | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paused WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def by_ref(self, ref: int) -> PausedTask | None:
        """The open task addressed by ``#ref`` (None if no open task holds it)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paused WHERE ref = ? AND state = 'open'", (ref,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def open_tasks(self) -> list[PausedTask]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paused WHERE state = 'open' ORDER BY ref"
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def expired(self, before: str) -> list[PausedTask]:
        """Open tasks paused strictly before the ISO timestamp ``before``."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paused WHERE state = 'open' AND paused_at < ?",
                (before,),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def load_state(self, task_id: str) -> StromboliState | None:
        """Re-hydrate the persisted :class:`StromboliState` for a resume."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM paused WHERE task_id = ?", (task_id,)
            ).fetchone()
        return StromboliState.model_validate_json(row["state_json"]) if row else None


def _row_to_task(row: sqlite3.Row) -> PausedTask:
    return PausedTask(
        task_id=row["task_id"], ref=row["ref"], reason=row["reason"],
        paused_at=row["paused_at"], state=row["state"],
        worktree_path=row["worktree_path"], session_id=row["session_id"],
        chat_id=row["chat_id"], guidance=row["guidance"], name=row["name"],
    )


__all__ = ["PausedIndex", "PausedState", "PausedTask"]
