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
        """Top-k semantic snippets to seed the Spec node (§6.2/§7).

        Small and bounded — never the whole store (retrieve, don't accumulate).
        Distilled lessons are recalled at the *planner* instead (see
        :meth:`recall_lessons`), so Spec carries only repo conventions.
        """
        hits: list[MemoryHit] = list(self.semantic.recall(query, k=k))
        return [h.document for h in hits]

    def recall_lessons(self, query: str, *, k: int = 3) -> list[str]:
        """Top-k distilled lessons for the planner (design: context-as-state).

        Filtered to ``kind="lesson"`` so the planner sees validated remedies
        ("fix that worked"), not raw traces — the sharp, decision-relevant
        signal injected at the start of a matching future run.
        """
        return [h.document for h in self.episodic.recall_lessons(query, k=k)]

    def recall_skills(self, query: str, *, k: int = 3) -> list[str]:
        """Top-k *approved* reusable skills for the planner (self-improving §3).

        Only eval-promoted skills are returned, so an unvetted candidate can't
        reach the coder. Bounded top-k — retrieve, don't accumulate.
        """
        return [h.document for h in self.procedural.recall_approved(query, k=k)]

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
