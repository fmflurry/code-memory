"""Embedding backends.

Two backends, same :class:`HybridVec` shape:

* :class:`OllamaEmbedder` (default) — dense-only via the Ollama daemon.
  Keeps the model warm across short-lived CLI processes (per-save
  reingest hooks, git hooks). Returns ``sparse`` as an empty vector.
* :class:`M3Embedder` (opt-in via ``EMBED_BACKEND=flagembed``) — dense
  + sparse from one in-process FlagEmbedding forward pass. Best for
  long-lived processes (watcher, MCP server) where the cold-load cost
  is paid once.

All backends are transparently wrapped in :class:`CachedEmbedder` so
content-hash cache hits skip the model entirely on re-ingest.

Use :func:`get_embedder` for the process-singleton; it reads
``EMBED_BACKEND`` and dispatches accordingly.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from ..config import CONFIG
from .cache import EmbedCache, hash_chunk
from .m3 import HybridVec, M3Embedder, SparseVec
from .ollama import OllamaEmbedder

log = logging.getLogger(__name__)

ENV_BACKEND = "EMBED_BACKEND"
ENV_DISABLE_CACHE = "EMBED_CACHE_DISABLED"


class Embedder(Protocol):
    """Common shape: produce :class:`HybridVec` per text."""

    def embed(self, texts):  # type: ignore[no-untyped-def]
        ...

    def embed_one(self, text: str) -> HybridVec: ...


class CachedEmbedder:
    """Embedder that consults a content-hash cache before the inner backend.

    Same ``embed`` / ``embed_one`` shape as the underlying embedder, so
    callers don't see the cache. The wrapper:

    1. Hashes every requested chunk.
    2. Pulls cached vectors in one ``IN (?, ?, …)`` SELECT.
    3. Forwards the miss list to the inner embedder.
    4. Writes the new vectors back to the cache before returning.
    5. Reassembles the output in the original input order.

    On a re-ingest where every chunk is unchanged, the inner embedder
    sees an empty list and returns instantly — the whole vector
    pipeline collapses to a SQLite scan + Qdrant upsert.
    """

    def __init__(
        self,
        inner: Embedder,
        cache: EmbedCache,
        model_id: str,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._model_id = model_id

    @property
    def cache(self) -> EmbedCache:
        return self._cache

    @property
    def model_id(self) -> str:
        return self._model_id

    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        if not texts:
            return []
        hashes = [hash_chunk(t) for t in texts]
        cached = self._cache.get_many(hashes, self._model_id)
        # Build miss-list + remember positions so we can splice results
        # back into input order.
        miss_positions: list[int] = []
        miss_texts: list[str] = []
        miss_hashes: list[str] = []
        for i, h in enumerate(hashes):
            if h not in cached:
                miss_positions.append(i)
                miss_texts.append(texts[i])
                miss_hashes.append(h)

        new_vecs: list[HybridVec] = (
            self._inner.embed(miss_texts) if miss_texts else []
        )
        if new_vecs:
            self._cache.put_many(
                zip(miss_hashes, new_vecs, strict=True),
                model=self._model_id,
            )

        # Reassemble in original order.
        out: list[HybridVec] = [None] * len(texts)  # type: ignore[list-item]
        for i, h in enumerate(hashes):
            if h in cached:
                out[i] = cached[h]
        for pos, vec in zip(miss_positions, new_vecs, strict=True):
            out[pos] = vec
        return out  # type: ignore[return-value]

    def embed_one(self, text: str) -> HybridVec:
        return self.embed([text])[0]

    def close(self) -> None:
        inner_close = getattr(self._inner, "close", None)
        if callable(inner_close):
            inner_close()
        self._cache.close()


_SINGLETON: Embedder | None = None


def _resolve_backend() -> str:
    raw = os.environ.get(ENV_BACKEND, "ollama").strip().lower()
    if raw in ("flagembed", "flag", "m3", "fastembed"):
        return "flagembed"
    return "ollama"


def _cache_enabled() -> bool:
    raw = os.environ.get(ENV_DISABLE_CACHE, "").strip().lower()
    return raw not in ("1", "true", "yes", "on")


def _build_inner_embedder(backend: str) -> tuple[Embedder, str]:
    """Return (embedder, model_id). model_id namespaces the cache."""
    if backend == "flagembed":
        log.info("embed: backend=flagembed (in-process m3, dense+sparse)")
        emb = M3Embedder()
        return emb, f"flagembed:{getattr(emb, 'model_name', 'bge-m3')}"
    log.info("embed: backend=ollama (HTTP, dense-only)")
    emb = OllamaEmbedder()
    return emb, f"ollama:{getattr(emb, 'model', 'bge-m3')}"


def get_embedder() -> Embedder:
    """Process-singleton embedder. First call wins the backend choice.

    The embedder is always wrapped in :class:`CachedEmbedder` unless
    ``EMBED_CACHE_DISABLED=1`` is set — content-hash cache hits then
    bypass the inner model entirely on re-ingest.
    """
    global _SINGLETON
    if _SINGLETON is None:
        backend = _resolve_backend()
        inner, model_id = _build_inner_embedder(backend)
        if not _cache_enabled():
            log.info("embed: cache disabled via %s", ENV_DISABLE_CACHE)
            _SINGLETON = inner
        else:
            cache_path = _cache_db_path()
            log.info("embed: cache at %s (model=%s)", cache_path, model_id)
            cache = EmbedCache(cache_path)
            _SINGLETON = CachedEmbedder(inner=inner, cache=cache, model_id=model_id)
    return _SINGLETON


def _cache_db_path() -> Path:
    """Cache file lives in ``CONFIG.data_dir`` so it survives ``code-memory
    reset`` (which only clears the project namespace) and so the same
    content embedded twice across projects reuses the cached vector.
    """
    base = Path(CONFIG.data_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base / "embed_cache.sqlite"


def set_embedder_for_tests(embedder: Embedder | None) -> None:
    global _SINGLETON
    _SINGLETON = embedder


__all__ = [
    "CachedEmbedder",
    "EmbedCache",
    "Embedder",
    "HybridVec",
    "M3Embedder",
    "OllamaEmbedder",
    "SparseVec",
    "get_embedder",
    "hash_chunk",
    "set_embedder_for_tests",
]
