"""Persistent embedding cache + CachedEmbedder wrapper.

The cache is the highest-impact lever for enterprise re-ingest
workloads. These tests pin the contract: same hash + same model =
cache hit (no inner call); same hash + different model = miss;
miss list is sent to inner embedder; result preserves input order;
written entries survive a cache reopen.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from code_memory.embed import CachedEmbedder, EmbedCache, hash_chunk
from code_memory.embed.m3 import HybridVec, SparseVec


def _vec(seed: float) -> HybridVec:
    return HybridVec(
        dense=[seed, seed + 0.1, seed + 0.2],
        sparse=SparseVec(indices=[int(seed)], values=[seed]),
    )


class _RecordingEmbedder:
    """Inner embedder stub that records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        self.calls.append(list(texts))
        # Map text → deterministic vector so callers can verify identity.
        return [_vec(float(len(t))) for t in texts]

    def embed_one(self, text: str) -> HybridVec:
        return self.embed([text])[0]


# ---------------------------------------------------------------- EmbedCache


def test_cache_returns_empty_when_no_entries(tmp_path: Path) -> None:
    cache = EmbedCache(tmp_path / "c.db")
    assert cache.get_many(["h1", "h2"], "m") == {}


def test_cache_round_trips_dense_and_sparse(tmp_path: Path) -> None:
    cache = EmbedCache(tmp_path / "c.db")
    v = HybridVec(
        dense=[0.1, 0.2, 0.3],
        sparse=SparseVec(indices=[1, 5, 9], values=[0.5, 0.7, 0.9]),
    )
    cache.put_many([("h1", v)], model="bge-m3")
    got = cache.get_many(["h1"], "bge-m3")["h1"]
    assert got.dense == pytest.approx([0.1, 0.2, 0.3])
    assert got.sparse.indices == [1, 5, 9]
    assert got.sparse.values == pytest.approx([0.5, 0.7, 0.9])


def test_cache_namespaces_by_model(tmp_path: Path) -> None:
    cache = EmbedCache(tmp_path / "c.db")
    cache.put_many([("h1", _vec(1.0))], model="bge-m3")
    assert "h1" in cache.get_many(["h1"], "bge-m3")
    assert cache.get_many(["h1"], "bge-small") == {}


def test_cache_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with EmbedCache(db) as cache:
        cache.put_many([("h1", _vec(1.0))], model="m")
    with EmbedCache(db) as cache2:
        assert "h1" in cache2.get_many(["h1"], "m")


def test_cache_stats_track_hits_and_misses(tmp_path: Path) -> None:
    cache = EmbedCache(tmp_path / "c.db")
    cache.put_many([("h1", _vec(1.0))], model="m")
    cache.get_many(["h1", "h2", "h3"], "m")  # 1 hit, 2 miss
    s = cache.stats()
    assert s["hits"] == 1
    assert s["misses"] == 2
    assert s["total_entries"] == 1


# -------------------------------------------------------- CachedEmbedder


def test_cached_embedder_hits_skip_inner(tmp_path: Path) -> None:
    inner = _RecordingEmbedder()
    cache = EmbedCache(tmp_path / "c.db")
    ce = CachedEmbedder(inner=inner, cache=cache, model_id="m")

    # First call — all misses, inner sees everything.
    ce.embed(["alpha", "beta"])
    assert inner.calls == [["alpha", "beta"]]

    # Second call with same inputs — all hits, inner sees nothing.
    inner.calls.clear()
    ce.embed(["alpha", "beta"])
    assert inner.calls == []


def test_cached_embedder_only_embeds_misses(tmp_path: Path) -> None:
    inner = _RecordingEmbedder()
    cache = EmbedCache(tmp_path / "c.db")
    ce = CachedEmbedder(inner=inner, cache=cache, model_id="m")

    ce.embed(["alpha"])  # warm "alpha"
    inner.calls.clear()

    ce.embed(["alpha", "beta", "gamma"])
    # Only the new texts hit the inner embedder.
    assert inner.calls == [["beta", "gamma"]]


def test_cached_embedder_preserves_input_order(tmp_path: Path) -> None:
    inner = _RecordingEmbedder()
    cache = EmbedCache(tmp_path / "c.db")
    ce = CachedEmbedder(inner=inner, cache=cache, model_id="m")

    # Warm two items.
    ce.embed(["alpha", "gamma"])
    inner.calls.clear()

    # Interleave hits and misses. Result must align with input texts.
    result = ce.embed(["beta", "alpha", "delta", "gamma"])
    assert len(result) == 4
    # The two warmed items map to their cached vectors; the two misses
    # map to vectors freshly produced by `_RecordingEmbedder`.
    assert result[0].dense[0] == float(len("beta"))    # miss
    assert result[1].dense[0] == float(len("alpha"))   # hit
    assert result[2].dense[0] == float(len("delta"))   # miss
    assert result[3].dense[0] == float(len("gamma"))   # hit


def test_cached_embedder_handles_empty_input(tmp_path: Path) -> None:
    inner = _RecordingEmbedder()
    cache = EmbedCache(tmp_path / "c.db")
    ce = CachedEmbedder(inner=inner, cache=cache, model_id="m")
    assert ce.embed([]) == []
    assert inner.calls == []


def test_hash_chunk_is_stable() -> None:
    assert hash_chunk("hello") == hash_chunk("hello")
    assert hash_chunk("hello") != hash_chunk("hello ")
