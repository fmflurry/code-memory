"""BGE-M3 hybrid embedder: dense + sparse from one forward pass.

Opt-in backend (``EMBED_BACKEND=flagembed``). Loads m3 in-process via
FlagEmbedding, which means each Python process pays a ~5-15 s
cold-load. Worth it for long-lived processes (watcher, MCP server)
that want the sparse signal; not worth it for hook-driven per-save
CLI invocations — :class:`code_memory.embed.OllamaEmbedder` is the
default for that reason.

m3 emits three views per input:

* Dense (1024-d float) — semantic similarity (cosine).
* Sparse (token-id -> weight) — lexical/identifier signal akin to BM25
  but learned. Used for code search where exact symbol names matter.
* ColBERT multi-vec — not used here; cross-encoder rerank covers the
  late-interaction case.

Fusion happens server-side in Qdrant (RRF / DBSF), so both views are
stored alongside each chunk and combined at query time.
"""

from __future__ import annotations

import logging
import platform
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..config import CONFIG

log = logging.getLogger(__name__)

# FlagEmbedding requires a HF repo id. The legacy ``EMBED_MODEL`` env
# var used the Ollama short name (``bge-m3``), which HF rejects, so we
# remap it here. Other models pass through unchanged.
_OLLAMA_TO_HF = {"bge-m3": "BAAI/bge-m3"}
DEFAULT_MODEL = "BAAI/bge-m3"


def _resolve_model(name: str | None) -> str:
    raw = (name or CONFIG.embed_model or DEFAULT_MODEL).strip()
    return _OLLAMA_TO_HF.get(raw, raw)


@dataclass(frozen=True)
class SparseVec:
    """Sparse vector in Qdrant's (indices, values) layout."""

    indices: list[int]
    values: list[float]


@dataclass(frozen=True)
class HybridVec:
    dense: list[float]
    sparse: SparseVec


def _detect_device() -> str:
    """Best available accelerator; falls back to CPU."""
    try:
        import torch
    except ImportError:
        return "cpu"
    if platform.system() == "Darwin" and torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class M3Embedder:
    """Stateful BGE-M3 wrapper producing dense + sparse vectors.

    Heavy to construct (downloads + loads ~2.3GB on first use). Cache
    the instance for the process lifetime — see ``code_memory.embed``
    factory below.
    """

    def __init__(
        self,
        model: str | None = None,
        device: str | None = None,
        use_fp16: bool | None = None,
        batch_size: int = 12,
    ) -> None:
        from FlagEmbedding import BGEM3FlagModel

        self.model_name = _resolve_model(model)
        self.device = device or _detect_device()
        # fp16 only safe on CUDA/MPS; CPU stays at fp32 for numerical
        # stability + because some BLAS kernels don't support fp16.
        if use_fp16 is None:
            use_fp16 = self.device in ("cuda", "mps")
        self.batch_size = batch_size
        log.info(
            "m3: loading %s (device=%s fp16=%s)",
            self.model_name,
            self.device,
            use_fp16,
        )
        self._impl = BGEM3FlagModel(
            self.model_name,
            use_fp16=use_fp16,
            devices=self.device,
        )

    # ----------------------------------------------------------- batch

    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        if not texts:
            return []
        out = self._impl.encode(
            list(texts),
            batch_size=self.batch_size,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        dense = out["dense_vecs"]
        sparse = out["lexical_weights"]
        return [
            HybridVec(
                dense=list(map(float, dense[i])),
                sparse=_to_qdrant_sparse(sparse[i]),
            )
            for i in range(len(texts))
        ]

    def embed_one(self, text: str) -> HybridVec:
        return self.embed([text])[0]

    # ------------------------------------------------------------ misc

    def close(self) -> None:
        # FlagEmbedding has no explicit close; drop the reference to free
        # GPU mem on next gc cycle.
        self._impl = None

    def __enter__(self) -> M3Embedder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _to_qdrant_sparse(weights: dict[Any, Any]) -> SparseVec:
    """Convert m3 ``{token_id: weight}`` mapping to Qdrant sparse format.

    m3 returns numpy floats keyed by string token IDs. Qdrant wants
    plain ints and floats; the conversion is explicit so misbehaving
    inputs (negative weights, NaN) are dropped rather than poisoning the
    index.
    """
    indices: list[int] = []
    values: list[float] = []
    for tok, w in weights.items():
        try:
            idx = int(tok)
        except (TypeError, ValueError):
            continue
        val = float(w)
        if val <= 0.0 or val != val:  # drop NaN / non-positive
            continue
        indices.append(idx)
        values.append(val)
    return SparseVec(indices=indices, values=values)


# Factory + singleton live in ``code_memory.embed.__init__`` so the
# Ollama and M3 backends share one selection mechanism. ``M3Embedder``
# itself remains directly constructible for tests and for users who
# want to bypass the env-var dispatch.
