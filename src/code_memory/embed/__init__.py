"""Embedding backends.

Two backends, same :class:`HybridVec` shape:

* :class:`OllamaEmbedder` (default) — dense-only via the Ollama daemon.
  Keeps the model warm across short-lived CLI processes (per-save
  reingest hooks, git hooks). Returns ``sparse`` as an empty vector.
* :class:`M3Embedder` (opt-in via ``EMBED_BACKEND=flagembed``) — dense
  + sparse from one in-process FlagEmbedding forward pass. Best for
  long-lived processes (watcher, MCP server) where the cold-load cost
  is paid once.

Use :func:`get_embedder` for the process-singleton; it reads
``EMBED_BACKEND`` and dispatches accordingly.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

from .m3 import HybridVec, M3Embedder, SparseVec
from .ollama import OllamaEmbedder

log = logging.getLogger(__name__)

ENV_BACKEND = "EMBED_BACKEND"


class Embedder(Protocol):
    """Common shape: produce :class:`HybridVec` per text."""

    def embed(self, texts):  # type: ignore[no-untyped-def]
        ...

    def embed_one(self, text: str) -> HybridVec: ...


_SINGLETON: Embedder | None = None


def _resolve_backend() -> str:
    raw = os.environ.get(ENV_BACKEND, "ollama").strip().lower()
    if raw in ("flagembed", "flag", "m3", "fastembed"):
        return "flagembed"
    return "ollama"


def get_embedder() -> Embedder:
    """Process-singleton embedder. First call wins the backend choice.

    To force a backend in tests, use :func:`set_embedder_for_tests`.
    """
    global _SINGLETON
    if _SINGLETON is None:
        backend = _resolve_backend()
        if backend == "flagembed":
            log.info("embed: backend=flagembed (in-process m3, dense+sparse)")
            _SINGLETON = M3Embedder()
        else:
            log.info("embed: backend=ollama (HTTP, dense-only)")
            _SINGLETON = OllamaEmbedder()
    return _SINGLETON


def set_embedder_for_tests(embedder: Embedder | None) -> None:
    global _SINGLETON
    _SINGLETON = embedder


__all__ = [
    "Embedder",
    "HybridVec",
    "M3Embedder",
    "OllamaEmbedder",
    "SparseVec",
    "get_embedder",
    "set_embedder_for_tests",
]
