"""The three-tier memory layer (PRD §7) — retrieve, don't accumulate.

``Memory`` aggregates the procedural / semantic / episodic tiers over one
:class:`~stromboli.memory.store.MemoryStore` and exposes the read used by the
Spec node (:meth:`Memory.recall_for_spec`) and the writes used by the Memory
Write node (per-tier).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stromboli.memory.episodic import EpisodicMemory
from stromboli.memory.procedural import ProceduralMemory
from stromboli.memory.semantic import SemanticMemory
from stromboli.memory.store import MemoryHit, MemoryStore


@dataclass
class Memory:
    """The aggregate memory facade over the three tiers."""

    store: MemoryStore

    def __post_init__(self) -> None:
        self.episodic = EpisodicMemory(self.store)
        self.semantic = SemanticMemory(self.store)
        self.procedural = ProceduralMemory(self.store)

    def recall_for_spec(self, query: str, *, k: int = 3) -> list[str]:
        """Top-k episodic + semantic snippets to seed the Spec node (§6.2/§7).

        Small and bounded — never the whole store (retrieve, don't accumulate).
        """
        hits: list[MemoryHit] = [
            *self.episodic.recall(query, k=k),
            *self.semantic.recall(query, k=k),
        ]
        return [h.document for h in hits]

    @classmethod
    def open(cls, persist_dir: str | Path | None = None) -> Memory:
        """Open the persistent memory store under ``persist_dir``."""
        return cls(MemoryStore(persist_dir=persist_dir))


__all__ = [
    "EpisodicMemory",
    "Memory",
    "MemoryHit",
    "MemoryStore",
    "ProceduralMemory",
    "SemanticMemory",
]
