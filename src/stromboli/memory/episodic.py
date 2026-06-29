"""Episodic memory (PRD §7) — task traces + verifier reflections.

Written by the Verifier (reflections) and Memory Write (traces); read by the
Spec node. Entries carry a timestamp so retrieval can weight recency / decay.
This is the loop-closer: a revise/escalate reflection is retrieved at the Spec
stage of the next similar task.
"""

from __future__ import annotations

from collections.abc import Sequence

from stromboli.memory.store import EPISODIC, MemoryHit, MemoryStore


class EpisodicMemory:
    """Reflections + traces, keyed by task and timestamped for recency."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def record_reflection(self, task_id: str, text: str, *, ts: float, seq: int = 0) -> str:
        entry_id = f"{task_id}:reflection:{seq}"
        self._store.add(
            EPISODIC,
            id=entry_id,
            document=text,
            metadata={"task_id": task_id, "kind": "reflection", "ts": ts},
        )
        return entry_id

    def record_trace(self, task_id: str, summary: str, *, ts: float) -> str:
        entry_id = f"{task_id}:trace"
        self._store.add(
            EPISODIC,
            id=entry_id,
            document=summary,
            metadata={"task_id": task_id, "kind": "trace", "ts": ts},
        )
        return entry_id

    def recall(self, query: str, *, k: int = 3) -> Sequence[MemoryHit]:
        return self._store.query(EPISODIC, text=query, k=k)


__all__ = ["EpisodicMemory"]
