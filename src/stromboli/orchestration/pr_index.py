"""Persistent index of Stromboli-opened PRs the feedback loop watches.

An opened PR is a *checkpoint*, not the finish line (design:
docs/design-pr-feedback-loop.md): failing CI and human review comments drive a
bounded fix cycle. This SQLite index is what lets the watcher survive a restart
and still know which PRs to sweep, how many fix rounds each has had, and the
watermarks (last CI SHA acted on, last comment timestamp) that keep the sweep
idempotent.

It is *not* the LangGraph checkpointer and not the investigate loop's
``PausedIndex`` — it holds the small amount of durable bookkeeping the sweep
needs, keyed by ``task_id``. The coder ``session_id`` is stored so a fix cycle
resumes the same agent session rather than starting cold.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

#: PR watch lifecycle.
WATCHING = "watching"
FIXING = "fixing"
ESCALATED = "escalated"
CLOSED = "closed"


@dataclass(frozen=True)
class PRWatch:
    """One watched PR's durable state."""

    task_id: str
    repo: str  # owner/name
    branch: str
    pr_number: int
    pr_url: str
    goal: str
    session_id: str | None
    fix_rounds: int = 0
    last_ci_sha: str | None = None
    last_comment_at: str | None = None
    state: str = WATCHING
    updated_at: str | None = None


class PRIndex:
    """SQLite-backed store of watched PRs (one row per task)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path))
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS pr_watch (
                task_id         TEXT PRIMARY KEY,
                repo            TEXT NOT NULL,
                branch          TEXT NOT NULL,
                pr_number       INTEGER NOT NULL,
                pr_url          TEXT NOT NULL,
                goal            TEXT NOT NULL DEFAULT '',
                session_id      TEXT,
                fix_rounds      INTEGER NOT NULL DEFAULT 0,
                last_ci_sha     TEXT,
                last_comment_at TEXT,
                state           TEXT NOT NULL DEFAULT 'watching',
                updated_at      TEXT
            )
            """
        )
        self._db.commit()

    def register(self, watch: PRWatch, *, now: str) -> None:
        """Add or replace a watch (idempotent on ``task_id``)."""
        row = replace(watch, updated_at=now)
        self._db.execute(
            """
            INSERT INTO pr_watch (task_id, repo, branch, pr_number, pr_url, goal,
                                  session_id, fix_rounds, last_ci_sha,
                                  last_comment_at, state, updated_at)
            VALUES (:task_id, :repo, :branch, :pr_number, :pr_url, :goal,
                    :session_id, :fix_rounds, :last_ci_sha, :last_comment_at,
                    :state, :updated_at)
            ON CONFLICT(task_id) DO UPDATE SET
                repo=excluded.repo, branch=excluded.branch,
                pr_number=excluded.pr_number, pr_url=excluded.pr_url,
                goal=excluded.goal, session_id=excluded.session_id,
                state=excluded.state, updated_at=excluded.updated_at
            """,
            row.__dict__,
        )
        self._db.commit()

    def active(self) -> list[PRWatch]:
        """Every PR still worth sweeping (not closed)."""
        cur = self._db.execute(
            "SELECT * FROM pr_watch WHERE state != ? ORDER BY updated_at", (CLOSED,)
        )
        return [self._row(r) for r in cur.fetchall()]

    def update(self, task_id: str, *, now: str, **fields: object) -> None:
        """Patch selected columns of a watch."""
        if not fields:
            return
        fields["updated_at"] = now
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        params = {**fields, "task_id": task_id}
        self._db.execute(
            f"UPDATE pr_watch SET {assignments} WHERE task_id = :task_id", params
        )
        self._db.commit()

    @staticmethod
    def _row(r: sqlite3.Row) -> PRWatch:
        return PRWatch(
            task_id=r["task_id"], repo=r["repo"], branch=r["branch"],
            pr_number=r["pr_number"], pr_url=r["pr_url"], goal=r["goal"],
            session_id=r["session_id"], fix_rounds=r["fix_rounds"],
            last_ci_sha=r["last_ci_sha"], last_comment_at=r["last_comment_at"],
            state=r["state"], updated_at=r["updated_at"],
        )


__all__ = [
    "CLOSED",
    "ESCALATED",
    "FIXING",
    "WATCHING",
    "PRIndex",
    "PRWatch",
]
