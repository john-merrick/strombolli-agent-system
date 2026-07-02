"""Procedural memory (PRD §7) — reusable skills the agent writes to itself.

The self-improvement skill library (self-improving §3): a resolved-with-
divergence pass distills a reusable *skill* ("what worked") here, and the
planner loads relevant skills on future tasks. To honour "a bad skill can't
silently degrade the system", a distilled skill enters as a **candidate** and
is only injected into the coder once an eval run has **promoted** it to
``approved`` — so nothing unvetted reaches the inner loop.

Written by Memory Write (candidates, on a verified pass only, so a broken
approach can't be reused); promoted by the offline skill gate; read (approved
only) by the planner via a bounded top-k retriever ("retrieve, don't
accumulate", CLAUDE.md).
"""

from __future__ import annotations

from collections.abc import Sequence

from stromboli.memory.store import PROCEDURAL, MemoryHit, MemoryStore

#: A distilled skill not yet validated — never injected into the coder.
STATUS_CANDIDATE = "candidate"
#: A skill an eval run promoted — eligible for injection.
STATUS_APPROVED = "approved"


class ProceduralMemory:
    """Verified, reusable code/skills, gated candidate → approved."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def add_skill(
        self,
        key: str,
        text: str,
        *,
        task_id: str,
        ts: float,
        status: str = STATUS_CANDIDATE,
        task_type: str = "",
    ) -> str:
        """Write a skill (default: an unvetted candidate). Id-keyed → re-adds
        overwrite, so promoting is just a re-add with ``status=approved``."""
        entry_id = f"skill:{key}"
        self._store.add(
            PROCEDURAL,
            id=entry_id,
            document=text,
            metadata={
                "task_id": task_id, "ts": ts, "status": status,
                "task_type": task_type, "key": key,
            },
        )
        return entry_id

    def promote(self, key: str, *, ts: float) -> bool:
        """Mark a candidate skill approved (validated by an eval gate).

        Returns True if the skill existed and was promoted.
        """
        entry_id = f"skill:{key}"
        hit = self._store.get(PROCEDURAL, entry_id)
        if hit is None:
            return False
        self._store.add(
            PROCEDURAL,
            id=entry_id,
            document=hit.document,
            metadata={**dict(hit.metadata), "status": STATUS_APPROVED, "ts": ts},
        )
        return True

    def recall(self, query: str, *, k: int = 3) -> Sequence[MemoryHit]:
        """Top-k skills of any status (used by the gate to fetch a candidate)."""
        return self._store.query(PROCEDURAL, text=query, k=k)

    def recall_approved(self, query: str, *, k: int = 3) -> Sequence[MemoryHit]:
        """Top-k *approved* skills — the only ones the planner injects."""
        return self._store.query(
            PROCEDURAL, text=query, k=k, where={"status": STATUS_APPROVED}
        )


__all__ = ["STATUS_APPROVED", "STATUS_CANDIDATE", "ProceduralMemory"]
