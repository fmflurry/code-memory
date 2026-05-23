"""Cross-encoder rerank stage (Metal/CUDA auto, CPU disabled by default).

Stage 2 of retrieval: rescore top-N bi-encoder candidates by feeding
``(query, code_text)`` pairs through a transformer cross-encoder. Heavier
than cosine sim but bounded — fires only on a small candidate set, once
per ``retrieve`` call.

Policy is auto-detected so users on Apple Silicon / CUDA boxes get the
quality lift for free, while CPU-only hosts (cold Macs Intel, Linux CI,
Docker) keep the existing heuristic-only path unchanged.
"""

from __future__ import annotations

import logging
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..vector import VectorHit

log = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_ALPHA = 0.5  # 0=bi-encoder only, 1=cross-encoder only
ENV_MODE = "CODEMEMORY_RERANK"  # auto | 1 | 0
ENV_MODEL = "CODEMEMORY_RERANK_MODEL"
ENV_ALPHA = "CODEMEMORY_RERANK_ALPHA"


def _resolve_alpha() -> float:
    """Blend weight for cross-encoder score: final = (1-α)·bi + α·ce."""
    raw = os.environ.get(ENV_ALPHA, "").strip()
    if not raw:
        return DEFAULT_ALPHA
    try:
        a = float(raw)
    except ValueError:
        return DEFAULT_ALPHA
    return min(max(a, 0.0), 1.0)


# ---------------------------------------------------------------- policy


@dataclass(frozen=True)
class RerankPolicy:
    enabled: bool
    device: str  # "mps" | "cuda" | "cpu" | "off"
    model: str
    reason: str


def _detect_device() -> str:
    """Return best available accelerator without importing torch eagerly."""
    try:
        import torch
    except ImportError:
        return "off"
    if platform.system() == "Darwin" and torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def decide_policy() -> RerankPolicy:
    """Resolve env + hardware into an enable/disable decision.

    - ``auto`` (default): on iff Metal or CUDA available; CPU stays off.
    - ``1``: force on (even CPU).
    - ``0``: force off.
    """
    mode = os.environ.get(ENV_MODE, "auto").strip().lower()
    model = os.environ.get(ENV_MODEL, DEFAULT_MODEL).strip() or DEFAULT_MODEL

    if mode == "0" or mode == "false" or mode == "off":
        return RerankPolicy(False, "off", model, "disabled via env")

    device = _detect_device()
    if mode == "1" or mode == "true" or mode == "on":
        if device == "off":
            return RerankPolicy(False, "off", model, "forced on but torch missing")
        return RerankPolicy(True, device, model, "forced on")

    # auto
    if device in ("mps", "cuda"):
        return RerankPolicy(True, device, model, "accelerator detected")
    return RerankPolicy(False, device, model, "no accelerator")


# ---------------------------------------------------------------- reranker iface


class Reranker(Protocol):
    def score(self, pairs: list[tuple[str, str]]) -> list[float]: ...


class _CrossEncoderReranker:
    """Adapter over ``sentence_transformers.CrossEncoder``.

    Preferred over ``FlagEmbedding.FlagReranker`` because the latter
    relies on ``tokenizer.prepare_for_model`` which was removed in
    ``transformers>=5``. CrossEncoder uses the modern tokenizer call.
    """

    def __init__(self, model: str, device: str) -> None:
        from sentence_transformers import CrossEncoder

        self._impl = CrossEncoder(model, device=device if device != "off" else None)

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        raw = self._impl.predict(
            list(pairs),
            convert_to_numpy=True,
            activation_fn=None,
        )
        # logits -> sigmoid normalization for comparability
        import math

        return [1.0 / (1.0 + math.exp(-float(s))) for s in raw]


_RERANKER: Reranker | None = None
_POLICY: RerankPolicy | None = None
_LOAD_FAILED = False


def _get_reranker() -> Reranker | None:
    """Lazy-load the reranker on first use; cache for the process lifetime."""
    global _RERANKER, _POLICY, _LOAD_FAILED
    if _LOAD_FAILED:
        return None
    if _RERANKER is not None:
        return _RERANKER
    policy = _POLICY or decide_policy()
    _POLICY = policy
    if not policy.enabled:
        return None
    try:
        _RERANKER = _CrossEncoderReranker(policy.model, policy.device)
        log.info(
            "rerank: cross-encoder on (model=%s device=%s reason=%s)",
            policy.model,
            policy.device,
            policy.reason,
        )
        return _RERANKER
    except ImportError:
        _LOAD_FAILED = True
        log.warning(
            "rerank: %s detected but FlagEmbedding not installed; "
            "install code-memory[rerank] to enable. Falling back to heuristics.",
            policy.device,
        )
        return None
    except Exception as e:  # noqa: BLE001 - never break retrieval on rerank failure
        _LOAD_FAILED = True
        log.warning("rerank: load failed (%s); falling back to heuristics.", e)
        return None


def set_reranker_for_tests(reranker: Reranker | None, policy: RerankPolicy | None = None) -> None:
    """Override the singleton reranker (test seam)."""
    global _RERANKER, _POLICY, _LOAD_FAILED
    _RERANKER = reranker
    _POLICY = policy
    _LOAD_FAILED = False


# ---------------------------------------------------------------- chunk text


def _read_chunk_text(payload: dict, *, max_chars: int = 4000) -> str | None:
    """Pull the source slice for a hit's ``path:start-end`` payload.

    Returns ``None`` when the file is missing or the range is empty so
    callers can drop unscorable hits cleanly.
    """
    path = payload.get("path")
    start = payload.get("start")
    end = payload.get("end")
    if not path or start is None or end is None:
        return None
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    s = max(int(start) - 1, 0)
    e = min(int(end), len(lines))
    if s >= e:
        return None
    text = "\n".join(lines[s:e])
    return text[:max_chars] if len(text) > max_chars else text


# ---------------------------------------------------------------- public API


def maybe_cross_encode(query: str, hits: list[VectorHit]) -> list[VectorHit]:
    """Blend bi-encoder and cross-encoder scores when enabled.

    Final score = ``(1 - α) · bi + α · ce`` where ``α`` is set via
    ``CODEMEMORY_RERANK_ALPHA`` (default 0.5). Hedges against
    cross-encoder regressions on queries where the bi-encoder was
    already right. Unscorable hits (missing files / empty ranges) keep
    their original bi-encoder score.

    No-op when policy is off, deps missing, or hits is empty.
    """
    if not hits:
        return hits
    reranker = _get_reranker()
    if reranker is None:
        return hits

    indexed: list[tuple[int, str]] = []
    for i, h in enumerate(hits):
        text = _read_chunk_text(h.payload)
        if text is not None:
            indexed.append((i, text))

    if not indexed:
        return hits

    pairs = [(query, text) for _, text in indexed]
    try:
        scores = reranker.score(pairs)
    except Exception as e:  # noqa: BLE001
        log.warning("rerank: score() failed (%s); using bi-encoder scores.", e)
        return hits

    alpha = _resolve_alpha()
    ce_by_idx: dict[int, float] = dict(zip([i for i, _ in indexed], scores, strict=True))
    return [
        VectorHit(
            id=h.id,
            score=(1.0 - alpha) * h.score + alpha * ce_by_idx[i] if i in ce_by_idx else h.score,
            payload=h.payload,
        )
        for i, h in enumerate(hits)
    ]
