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

import logging

import httpx

from ..config import CONFIG
from ..resilience import with_retry
from .m3 import HybridVec, SparseVec

log_ = logging.getLogger(__name__)


class OllamaEmbedder:
    """Thin sync wrapper over Ollama /api/embed.

    Returns :class:`HybridVec` with an empty sparse component so the
    shape matches :class:`M3Embedder`. The empty sparse vector is a
    deliberate signal to :class:`QdrantStore` that hybrid fusion will
    degrade to dense-only for this point.
    """

    # Default connect timeout: 5 s is generous for a loopback service
    # but still short enough that a wrong-stack (IPv6 vs IPv4) or
    # misconfigured host fails fast.  The read timeout is kept long
    # (300 s) because Ollama loads the model on the first request — that
    # cold-load phase happens during the *read* phase, not the connect.
    # With with_retry(max_retries=3) the worst-case wall time drops from
    # ~1 200 s (4 × 300 s connect hangs) to ~15 s (3 × 5 s retries).
    _DEFAULT_CONNECT_TIMEOUT: float = 5.0
    _DEFAULT_READ_TIMEOUT: float = 300.0

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = _DEFAULT_READ_TIMEOUT,
    ) -> None:
        self.url = (url or CONFIG.ollama_url).rstrip("/")
        self.model = model or CONFIG.embed_model
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        )
        self._client = httpx.Client(timeout=timeout)

    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        if not texts:
            return []

        def _call():
            # keep_alive per request: on a long CPU-bound ingest the
            # server-side default (5 m, sliding only when a request
            # *completes*) can expire while a large batch is queued —
            # Ollama then unloads the runner under the pending requests,
            # which are never answered (the connection just sits open).
            res = self._client.post(
                f"{self.url}/api/embed",
                json={
                    "model": self.model,
                    "input": list(texts),
                    "keep_alive": "30m",
                },
            )
            res.raise_for_status()
            data = res.json()
            embeddings = data.get("embeddings")
            if embeddings is None:
                raise RuntimeError(f"Ollama returned no embeddings: {data}")
            return embeddings

        embeddings = with_retry(
            _call,
            max_retries=3,
            backoff_s=1.0,
            on_retry=lambda attempt, exc: log_.warning(
                "ollama embed retry %d/3 after %s", attempt, exc
            ),
        )

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
