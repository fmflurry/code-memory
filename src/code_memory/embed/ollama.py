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
        # Memoized on first success only — see model_digest(). A failed
        # resolution must never stick, or transient Ollama downtime at
        # process start would permanently poison a long-lived MCP server.
        self._digest: str | None = None

    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        if not texts:
            return []

        def _call():
            res = self._client.post(
                f"{self.url}/api/embed",
                json={"model": self.model, "input": list(texts)},
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

    def model_digest(self) -> str:
        """Resolve and memoize this model's content digest via Ollama's
        ``/api/tags``.

        The embed-cache key pins this digest so a ``:latest`` re-pull
        that swaps the underlying weights can't silently mix old and
        new vectors under an unchanged model-name key. Hard-fails
        (raises ``RuntimeError``) when Ollama is unreachable, the
        response is malformed, or the configured model isn't present —
        deliberately, since falling back to a digest-less key would
        reintroduce that exact silent-corruption bug. Only a
        *successful* resolution is memoized; a failure must not stick,
        so transient downtime doesn't permanently poison a long-lived
        process.
        """
        if self._digest is not None:
            return self._digest

        try:
            res = self._client.get(f"{self.url}/api/tags")
            res.raise_for_status()
            data = res.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Ollama unreachable while resolving model digest for "
                f"{self.model!r} via {self.url}/api/tags: {exc}"
            ) from exc

        models = data.get("models") or []
        for entry in models:
            name = entry.get("name", "")
            if name == self.model or name.split(":", 1)[0] == self.model:
                digest = entry.get("digest")
                if not digest:
                    raise RuntimeError(
                        f"Ollama /api/tags entry for model {self.model!r} "
                        f"has no digest: {entry!r}"
                    )
                self._digest = digest
                return digest

        raise RuntimeError(
            f"model {self.model!r} not found in Ollama /api/tags response "
            f"({self.url}/api/tags) — is it pulled?"
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OllamaEmbedder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
