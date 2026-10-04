"""Deterministic fakes for retrieval tests — no model downloads, no GPU.

HashEmbedder maps text to a normalized bag-of-words hash vector (the "hashing
trick"), so texts sharing words get high cosine similarity. That makes dense
search *meaningful* in tests without fastembed: a query about "rocm build"
genuinely lands nearest the chunk that talks about ROCm builds.
"""

import hashlib
import math


class HashEmbedder:
    """Drop-in for cognita.embeddings.Embedder (embed() only)."""

    def __init__(self, dimensions: int = 32):
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for word in text.lower().split():
            h = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big")
            vec[h % self.dimensions] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


class OverlapReranker:
    """Drop-in for cognita.embeddings.Reranker: scores by word overlap."""

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        q = set(query.lower().split())
        return [len(q & set(t.lower().split())) / (len(q) or 1) for t in texts]


class BrokenReranker:
    """A reranker whose model is unavailable — search must fall back to RRF order."""

    def rerank(self, query: str, texts: list[str]) -> None:
        return None
