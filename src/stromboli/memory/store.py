"""The ChromaDB-backed memory store — one collection per tier (PRD §7).

Principle: **retrieve, don't accumulate.** Callers pull a small top-k under a
token budget; the store never returns the whole tier. It is a thin shell over a
Chroma client: collections are created lazily per tier, and the client +
embedding function are injectable so tests run against an in-memory client with
a deterministic embedding (no model download, no network).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The three memory tiers (PRD §7); also the Chroma collection names.
PROCEDURAL = "procedural"
SEMANTIC = "semantic"
EPISODIC = "episodic"
TIERS = (PROCEDURAL, SEMANTIC, EPISODIC)


@dataclass(frozen=True)
class MemoryHit:
    """One retrieved memory entry."""

    id: str
    document: str
    metadata: dict[str, Any]
    distance: float


class MemoryStore:
    """A persistent, per-tier vector store over a Chroma client."""

    def __init__(
        self,
        *,
        persist_dir: str | Path | None = None,
        client: Any | None = None,
        embedding_function: Any | None = None,
    ) -> None:
        if client is None:
            import chromadb

            if persist_dir is None:
                client = chromadb.EphemeralClient()
            else:
                Path(persist_dir).mkdir(parents=True, exist_ok=True)
                client = chromadb.PersistentClient(path=str(persist_dir))
        self._client = client
        self._ef = embedding_function
        self._collections: dict[str, Any] = {}

    def _collection(self, tier: str) -> Any:
        if tier not in self._collections:
            kwargs: dict[str, Any] = {"name": tier}
            if self._ef is not None:
                kwargs["embedding_function"] = self._ef
            self._collections[tier] = self._client.get_or_create_collection(**kwargs)
        return self._collections[tier]

    def add(
        self, tier: str, *, id: str, document: str, metadata: dict[str, Any]
    ) -> None:
        """Upsert one entry into ``tier`` (id-keyed, so re-adds overwrite)."""
        # Chroma rejects empty metadata dicts; always carry the tier at minimum.
        meta = {"tier": tier, **metadata}
        self._collection(tier).upsert(
            ids=[id], documents=[document], metadatas=[meta]
        )

    def query(self, tier: str, *, text: str, k: int = 3) -> list[MemoryHit]:
        """Return up to top-``k`` entries in ``tier`` nearest to ``text``."""
        result = self._collection(tier).query(query_texts=[text], n_results=k)
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        hits: list[MemoryHit] = []
        for i, doc_id in enumerate(ids):
            hits.append(
                MemoryHit(
                    id=str(doc_id),
                    document=str(docs[i]) if i < len(docs) else "",
                    metadata=dict(metas[i]) if i < len(metas) and metas[i] else {},
                    distance=float(dists[i]) if i < len(dists) else 0.0,
                )
            )
        return hits


__all__ = [
    "EPISODIC",
    "PROCEDURAL",
    "SEMANTIC",
    "TIERS",
    "MemoryHit",
    "MemoryStore",
]
