"""A deterministic, offline embedding function for memory tests.

Embeds text as a hashed bag-of-words vector, so texts that **share words** land
near each other (and length doesn't dominate) — enough for nearest-neighbour
recall assertions without downloading a real embedding model.
"""

from __future__ import annotations

import re
from typing import Any

import chromadb
from chromadb.config import Settings

_DIM = 256
_WORD = re.compile(r"[a-z0-9]+")


class CharFreqEmbedding(chromadb.EmbeddingFunction[Any]):
    """Deterministic hashed bag-of-words embedding (no network, no model)."""

    def __init__(self) -> None:
        self._dim = _DIM

    @staticmethod
    def name() -> str:
        return "hashed-bow"

    def get_config(self) -> dict[str, Any]:
        return {}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> CharFreqEmbedding:
        return CharFreqEmbedding()

    def __call__(self, input: Any) -> Any:
        vectors: list[list[float]] = []
        for doc in input:
            vec = [0.0] * _DIM
            for word in _WORD.findall(doc.lower()):
                # Stable (non-salted) hash so the embedding is deterministic.
                bucket = sum(ord(c) for c in word) % _DIM
                vec[bucket] += 1.0
            vectors.append(vec)
        return vectors


def make_store() -> Any:
    """A fresh, isolated in-memory store (resets the in-process Chroma system)."""
    from stromboli.memory.store import MemoryStore

    # EphemeralClient shares one in-process system across instances, so reset to
    # guarantee each test starts from a clean slate.
    client = chromadb.Client(Settings(is_persistent=False, allow_reset=True))
    client.reset()
    return MemoryStore(client=client, embedding_function=CharFreqEmbedding())
