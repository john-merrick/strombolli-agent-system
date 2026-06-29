"""Procedural memory (PRD §7) — verified reusable skills.

Written by Memory Write **only on a verified pass** (never on a failure, so a
broken approach can't be reused); read by the Coding node. Default granularity:
extracted functions with tests (PRD §11.5).
"""

from __future__ import annotations

from collections.abc import Sequence

from stromboli.memory.store import PROCEDURAL, MemoryHit, MemoryStore


class ProceduralMemory:
    """Verified, reusable code/skills."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def add_skill(self, key: str, text: str, *, task_id: str, ts: float) -> str:
        entry_id = f"skill:{key}"
        self._store.add(
            PROCEDURAL,
            id=entry_id,
            document=text,
            metadata={"task_id": task_id, "ts": ts},
        )
        return entry_id

    def recall(self, query: str, *, k: int = 3) -> Sequence[MemoryHit]:
        return self._store.query(PROCEDURAL, text=query, k=k)


__all__ = ["ProceduralMemory"]
