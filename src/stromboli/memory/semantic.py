"""Semantic memory (PRD §7) — repo conventions & architecture decisions.

Notion is the source of truth; this tier mirrors conventions for retrieval. Read
by the Spec and Coding nodes; written by Memory Write or manually.
"""

from __future__ import annotations

from collections.abc import Sequence

from stromboli.memory.store import SEMANTIC, MemoryHit, MemoryStore


class SemanticMemory:
    """Durable repo conventions / architecture decisions."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def add_convention(self, key: str, text: str, *, source: str = "manual") -> str:
        entry_id = f"convention:{key}"
        self._store.add(
            SEMANTIC, id=entry_id, document=text, metadata={"source": source}
        )
        return entry_id

    def recall(self, query: str, *, k: int = 3) -> Sequence[MemoryHit]:
        return self._store.query(SEMANTIC, text=query, k=k)


__all__ = ["SemanticMemory"]
