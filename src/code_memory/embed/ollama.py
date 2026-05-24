"""Ollama-backed dense embedder (default backend).

Runs `bge-m3` (or any Ollama-served model) over HTTP. Ollama keeps the
model loaded in its own daemon, so short-lived CLI processes (e.g.
``code-memory reingest <file>`` invoked from a save-file hook) reuse
the warm model instead of paying a ~5-15 s cold load every call.

Trade-off vs the in-process FlagEmbedding path: Ollama only exposes the
dense head of m3 — no sparse, no ColBERT. Sparse is returned as an
empty :class:`SparseVec` so the Qdrant hybrid layout still upserts
cleanly; queries through the hybrid slot then degrade to dense-only at
RRF time. Users who want true m3 hybrid (dense + sparse from one
forward pass) can flip ``EMBED_BACKEND=flagembed`` and accept the
cold-load cost.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from ..config import CONFIG
from .m3 import HybridVec, SparseVec


class OllamaEmbedder:
    """Thin sync wrapper over Ollama /api/embed.

    Returns :class:`HybridVec` with an empty sparse component so the
    shape matches :class:`M3Embedder`. The empty sparse vector is a
    deliberate signal to :class:`QdrantStore` that hybrid fusion will
    degrade to dense-only for this point.
    """

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.url = (url or CONFIG.ollama_url).rstrip("/")
        self.model = model or CONFIG.embed_model
        self._client = httpx.Client(timeout=timeout)

    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        if not texts:
            return []
        res = self._client.post(
            f"{self.url}/api/embed",
            json={"model": self.model, "input": list(texts)},
        )
        res.raise_for_status()
        data = res.json()
        embeddings = data.get("embeddings")
        if embeddings is None:
            raise RuntimeError(f"Ollama returned no embeddings: {data}")
        empty = SparseVec(indices=[], values=[])
        return [
            HybridVec(dense=[float(x) for x in vec], sparse=empty)
            for vec in embeddings
        ]

    def embed_one(self, text: str) -> HybridVec:
        return self.embed([text])[0]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OllamaEmbedder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
