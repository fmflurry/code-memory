"""Embedding backends.

Three backends, same :class:`HybridVec` shape:

* :class:`OllamaEmbedder` (default) — dense-only via the Ollama daemon.
  Keeps the model warm across short-lived CLI processes (per-save
  reingest hooks, git hooks). Returns ``sparse`` as an empty vector.
* :class:`M3Embedder` (opt-in via ``EMBED_BACKEND=flagembed``) — dense
  + sparse from one in-process FlagEmbedding forward pass. Best for
  long-lived processes (watcher, MCP server) where the cold-load cost
  is paid once.
* :class:`TEIEmbedder` (opt-in via ``EMBED_BACKEND=tei``) — dense-only
  via HuggingFace's `text-embeddings-inference` GPU server. **5-10×
  cold-ingest speedup vs Ollama on Linux + NVIDIA**, same weights, no
  recall loss. Set ``TEI_URL`` to point at the TEI daemon (default
  ``http://localhost:8080``).

All backends are transparently wrapped in :class:`CachedEmbedder` so
content-hash cache hits skip the model entirely on re-ingest.

Use :func:`get_embedder` for the process-singleton; it reads
``EMBED_BACKEND`` and dispatches accordingly.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..config import CONFIG
from .cache import EmbedCache, hash_chunk
from .m3 import HybridVec, M3Embedder, SparseVec
from .ollama import OllamaEmbedder
from .tei import TEIEmbedder

if TYPE_CHECKING:
    from ..metrics import MetricsStore

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
        model_id: str | Callable[[], str],
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._model_id: str | Callable[[], str] = model_id

    @property
    def cache(self) -> EmbedCache:
        return self._cache

    @property
    def model_id(self) -> str:
        """Resolve ``model_id`` lazily on first access, then memoize.

        ``self._model_id`` starts as a plain string (TEI/FlagEmbed) or a
        zero-arg callable (Ollama — resolving the model digest requires
        an HTTP round trip, deferred until the first real ``embed()``
        call so ``--no-vectors`` ingests never need Ollama reachable).
        """
        value = self._model_id
        if callable(value):
            value = value()
            self._model_id = value
        return value

    def flush_cache_stats(self, metrics: "MetricsStore") -> None:
        """Record accumulated hit/miss counts and reset them.

        ``EmbedCache.hits``/``.misses`` are cumulative-since-open, not
        deltas — resetting after recording is load-bearing: without it,
        ``MetricsStore.summary()``'s ``SUM(hits)`` across ``cache_stats``
        rows would double-count on every subsequent flush.
        """
        hits, misses = self._cache.hits, self._cache.misses
        if hits == 0 and misses == 0:
            return
        metrics.record_cache_stats(hits=hits, misses=misses)
        self._cache.hits = 0
        self._cache.misses = 0

    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        if not texts:
            return []
        hashes = [hash_chunk(t) for t in texts]
        cached = self._cache.get_many(hashes, self.model_id)
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
                model=self.model_id,
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
# Guards the check-build-assign below so N threads racing to cold-start
# the embedder singleton (daemon startup, concurrent first requests)
# build exactly one backend instead of N models loaded into the process.
_SINGLETON_LOCK = threading.Lock()


def _resolve_backend() -> str:
    raw = os.environ.get(ENV_BACKEND, "ollama").strip().lower()
    if raw in ("flagembed", "flag", "m3", "fastembed"):
        return "flagembed"
    if raw in ("tei", "text-embeddings-inference"):
        return "tei"
    return "ollama"


def _cache_enabled() -> bool:
    raw = os.environ.get(ENV_DISABLE_CACHE, "").strip().lower()
    return raw not in ("1", "true", "yes", "on")


def _build_inner_embedder(backend: str) -> tuple[Embedder, str | Callable[[], str]]:
    """Return (embedder, model_id). model_id namespaces the cache.

    ``model_id`` is a plain string for FlagEmbed/TEI, but a zero-arg
    callable for Ollama: pinning the cache key to the resolved model
    *digest* (via Ollama's ``/api/tags``) requires a network round trip,
    and ``Pipeline.__init__`` calls :func:`get_embedder` unconditionally
    — even under ``--no-vectors``/``skip_vectors=True``, before the
    skip check runs. Resolving eagerly here would make a graph-only
    ingest require Ollama reachable for no reason, so resolution is
    deferred to :class:`CachedEmbedder`'s first real ``embed()`` call.

    Note: this also means Ollama and TEI no longer share a cache
    namespace — the digest concept is Ollama-specific, so the two
    backends can't produce an identical string key up front the way
    they used to.
    """
    if backend == "flagembed":
        log.info("embed: backend=flagembed (in-process m3, dense+sparse)")
        emb_m3 = M3Embedder()
        # FlagEmbed carries a sparse vector that Ollama/TEI don't —
        # keep its cache slot separate so dense-only backends never
        # see (and silently drop) those sparse rows.
        return emb_m3, f"flagembed:{getattr(emb_m3, 'model_name', 'bge-m3')}"
    if backend == "tei":
        log.info("embed: backend=tei (HTTP @ %s, dense-only)", CONFIG.tei_url)
        emb_tei = TEIEmbedder()
        return emb_tei, f"model:{getattr(emb_tei, 'model', 'bge-m3')}"
    log.info("embed: backend=ollama (HTTP, dense-only)")
    emb = OllamaEmbedder()
    return emb, lambda: f"model:{emb.model}@{emb.model_digest()}"


def get_embedder() -> Embedder:
    """Process-singleton embedder. First call wins the backend choice.

    The embedder is always wrapped in :class:`CachedEmbedder` unless
    ``EMBED_CACHE_DISABLED=1`` is set — content-hash cache hits then
    bypass the inner model entirely on re-ingest.
    """
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            backend = _resolve_backend()
            inner, model_id = _build_inner_embedder(backend)
            if not _cache_enabled():
                log.info("embed: cache disabled via %s", ENV_DISABLE_CACHE)
                _SINGLETON = inner
            else:
                cache_path = _cache_db_path()
                model_label = (
                    model_id if isinstance(model_id, str) else "<lazy, resolved on first embed>"
                )
                log.info("embed: cache at %s (model=%s)", cache_path, model_label)
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
    "TEIEmbedder",
    "get_embedder",
    "hash_chunk",
    "set_embedder_for_tests",
]
