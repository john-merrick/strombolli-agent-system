"""Durable failure store — the failure-to-dataset pipeline (self-improving §1).

Every terminal verdict (pass *and* fail) is recorded here at the graph's
terminal boundary, so the rejection signal the verifier produces — the input,
the diff, the test evidence, and the structured surprise
(expected/observed/cause/fix/task_type/failure_mode) — is captured durably
instead of consumed once in-run and discarded.

This is the foundation the other self-improvement loops read:

* **GEPA on the verifier** (§2) trains on these rows once a human accept/reject
  label is attached (:meth:`label`), exported into the eval-harness JSON shape
  (:meth:`export_verifier_dataset`).
* **The morning rundown** (§4) clusters unresolved rows by
  ``task_type``/``failure_mode`` and routes each cluster to the right loop.

It is a local SQLite store (``.stromboli/failures.db``), the same idiom as
``paused.db`` / ``prs.db`` — offline, joinable by ``task_id``, no network. The
Langfuse-dataset export is a thin optional add-on, not the source of truth.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

#: Human label values (accept = the verifier was right, reject = it was wrong).
LABEL_ACCEPT = "accept"
LABEL_REJECT = "reject"


@dataclass(frozen=True)
class FailureRecord:
    """One terminal verdict captured for learning."""

    task_id: str
    ts: str
    goal: str
    decision: str  # pass | revise | escalate (the verifier's call)
    outcome: str  # done | escalated | queued — the run's terminal status
    task_type: str = ""
    failure_mode: str = ""
    reason: str = ""
    expected: str = ""
    observed: str = ""
    cause: str = ""
    fix: str = ""
    diff: str = ""
    test_evidence: str = ""
    acceptance_criteria: str = ""  # newline-joined
    #: Human accept/reject on the verifier's decision (None until labelled).
    human_label: str | None = None
    #: Whether the run ultimately shipped (a verified pass → PR).
    resolved: bool = False


class FailureIndex:
    """SQLite store of terminal verdicts (one row per task, last write wins)."""

    #: Cap on stored diff/evidence so a runaway log can't bloat the DB.
    _MAX_FIELD = 8000

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path))
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS failures (
                task_id             TEXT PRIMARY KEY,
                ts                  TEXT NOT NULL,
                goal                TEXT NOT NULL DEFAULT '',
                decision            TEXT NOT NULL DEFAULT '',
                outcome             TEXT NOT NULL DEFAULT '',
                task_type           TEXT NOT NULL DEFAULT '',
                failure_mode        TEXT NOT NULL DEFAULT '',
                reason              TEXT NOT NULL DEFAULT '',
                expected            TEXT NOT NULL DEFAULT '',
                observed            TEXT NOT NULL DEFAULT '',
                cause               TEXT NOT NULL DEFAULT '',
                fix                 TEXT NOT NULL DEFAULT '',
                diff                TEXT NOT NULL DEFAULT '',
                test_evidence       TEXT NOT NULL DEFAULT '',
                acceptance_criteria TEXT NOT NULL DEFAULT '',
                human_label         TEXT,
                resolved            INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._db.commit()

    def record(self, rec: FailureRecord) -> None:
        """Insert or replace the terminal record for a task (idempotent)."""
        row = replace(
            rec,
            diff=rec.diff[: self._MAX_FIELD],
            test_evidence=rec.test_evidence[: self._MAX_FIELD],
        )
        data = {**row.__dict__, "resolved": int(row.resolved)}
        self._db.execute(
            """
            INSERT INTO failures (task_id, ts, goal, decision, outcome, task_type,
                failure_mode, reason, expected, observed, cause, fix, diff,
                test_evidence, acceptance_criteria, human_label, resolved)
            VALUES (:task_id, :ts, :goal, :decision, :outcome, :task_type,
                :failure_mode, :reason, :expected, :observed, :cause, :fix, :diff,
                :test_evidence, :acceptance_criteria, :human_label, :resolved)
            ON CONFLICT(task_id) DO UPDATE SET
                ts=excluded.ts, goal=excluded.goal, decision=excluded.decision,
                outcome=excluded.outcome, task_type=excluded.task_type,
                failure_mode=excluded.failure_mode, reason=excluded.reason,
                expected=excluded.expected, observed=excluded.observed,
                cause=excluded.cause, fix=excluded.fix, diff=excluded.diff,
                test_evidence=excluded.test_evidence,
                acceptance_criteria=excluded.acceptance_criteria,
                resolved=excluded.resolved
            """,
            data,
        )
        self._db.commit()

    def label(self, task_id: str, human_label: str) -> None:
        """Attach a human accept/reject to the verifier's call on this task."""
        self._db.execute(
            "UPDATE failures SET human_label = ? WHERE task_id = ?",
            (human_label, task_id),
        )
        self._db.commit()

    def all(self) -> list[FailureRecord]:
        """Every recorded terminal verdict, newest last."""
        cur = self._db.execute("SELECT * FROM failures ORDER BY ts")
        return [self._row(r) for r in cur.fetchall()]

    def unresolved(self) -> list[FailureRecord]:
        """Rejections/escalations that never shipped — the backlog's raw input."""
        cur = self._db.execute(
            "SELECT * FROM failures WHERE resolved = 0 ORDER BY ts"
        )
        return [self._row(r) for r in cur.fetchall()]

    def labelled(self) -> list[FailureRecord]:
        """Rows carrying a human accept/reject — the GEPA/eval trainset."""
        cur = self._db.execute(
            "SELECT * FROM failures WHERE human_label IS NOT NULL ORDER BY ts"
        )
        return [self._row(r) for r in cur.fetchall()]

    def export_verifier_dataset(self, path: str | Path, *, threshold: float = 0.7) -> int:
        """Write labelled rows to the eval-harness JSON shape. Returns the count.

        The label semantics: ``accept`` means the human agreed with the
        verifier's ``decision`` (so that decision is the gold label);
        ``reject`` means the human disagreed, so the gold label is *not* the
        verifier's decision — those rows are exported with the human's implied
        correction where derivable, else skipped (a reject with no corrected
        decision carries no usable gold label). This keeps the exported set a
        clean accept/reject trainset for the verifier optimizer (§2).
        """
        cases = []
        for r in self.labelled():
            gold = r.decision if r.human_label == LABEL_ACCEPT else None
            if gold is None:
                continue  # a bare reject has no derivable corrected label
            cases.append(
                {
                    "id": r.task_id,
                    "inputs": {
                        "goal": r.goal,
                        "acceptance_criteria": [
                            c for c in r.acceptance_criteria.split("\n") if c
                        ],
                        "diff": r.diff,
                        "tests_passed": "passed" in r.test_evidence.lower(),
                        "test_summary": r.test_evidence,
                    },
                    "expected": {"decision": gold},
                }
            )
        payload = {
            "name": "verifier_labelled",
            "metric": "agreement_with_human_labels",
            "threshold": threshold,
            "cases": cases,
        }
        Path(path).write_text(json.dumps(payload, indent=2))
        return len(cases)

    @staticmethod
    def _row(r: sqlite3.Row) -> FailureRecord:
        return FailureRecord(
            task_id=r["task_id"], ts=r["ts"], goal=r["goal"],
            decision=r["decision"], outcome=r["outcome"],
            task_type=r["task_type"], failure_mode=r["failure_mode"],
            reason=r["reason"], expected=r["expected"], observed=r["observed"],
            cause=r["cause"], fix=r["fix"], diff=r["diff"],
            test_evidence=r["test_evidence"],
            acceptance_criteria=r["acceptance_criteria"],
            human_label=r["human_label"], resolved=bool(r["resolved"]),
        )


__all__ = [
    "LABEL_ACCEPT",
    "LABEL_REJECT",
    "FailureIndex",
    "FailureRecord",
]
