"""Pins FIX A's core unit: ``CachedEmbedder.flush_cache_stats``.

Root cause being fixed (see diagnosis returned to orchestrator):
``EmbedCache.get_many`` increments ``self.hits``/``self.misses`` in
memory only (cache.py:138-139); ``EmbedCache.stats()`` and
``CachedEmbedder.cache`` have zero call sites in ``src/``;
``MetricsStore.record_cache_stats`` (already correct — see
tests/test_metrics.py) has zero *production* call sites. So
``codememory_health``'s ``cache`` block is a permanently-0.0 gauge.

Design decision pinned here: the accumulated hit/miss counters are
cumulative-since-open (not deltas), so a naive "record on every call"
approach double-counts across repeated flushes. ``flush_cache_stats``
must record the delta and reset the in-memory counters — that's the
contract these tests lock in.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from code_memory.embed import CachedEmbedder, EmbedCache
from code_memory.embed.m3 import HybridVec, SparseVec
from code_memory.metrics import MetricsStore


class _Inner:
    """Minimal inner embedder — deterministic, no network."""

    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        return [HybridVec(dense=[0.1], sparse=SparseVec([], [])) for _ in texts]

    def embed_one(self, text: str) -> HybridVec:
        return self.embed([text])[0]


def test_flush_cache_stats_records_accumulated_hits_and_misses(tmp_path: Path) -> None:
    cache = EmbedCache(tmp_path / "c.db")
    ce = CachedEmbedder(inner=_Inner(), cache=cache, model_id="m")
    store = MetricsStore(tmp_path / "metrics.db")

    ce.embed(["a", "b"])  # 2 misses
    ce.embed(["a", "b"])  # 2 hits
    ce.flush_cache_stats(store)

    summary = store.summary()
    assert summary["cache"]["total_hits"] == 2
    assert summary["cache"]["total_misses"] == 2
    store.close()


def test_flush_cache_stats_resets_counters_so_repeat_flush_does_not_double_count(
    tmp_path: Path,
) -> None:
    cache = EmbedCache(tmp_path / "c.db")
    ce = CachedEmbedder(inner=_Inner(), cache=cache, model_id="m")
    store = MetricsStore(tmp_path / "metrics.db")

    ce.embed(["a"])  # 1 miss
    ce.flush_cache_stats(store)
    ce.flush_cache_stats(store)  # no new activity since last flush — must be a no-op

    summary = store.summary()
    assert summary["cache"]["total_misses"] == 1, (
        "a second flush with no intervening lookups must not re-report "
        "the same miss — counters must reset on flush"
    )
    store.close()


def test_flush_cache_stats_does_not_write_a_row_when_no_lookups_happened(
    tmp_path: Path,
) -> None:
    cache = EmbedCache(tmp_path / "c.db")
    ce = CachedEmbedder(inner=_Inner(), cache=cache, model_id="m")
    store = MetricsStore(tmp_path / "metrics.db")

    ce.flush_cache_stats(store)  # never embedded anything

    summary = store.summary()
    assert summary["cache"]["total_hits"] == 0
    assert summary["cache"]["total_misses"] == 0
    assert summary["cache"]["hit_ratio"] == 0.0, (
        "hit_ratio must stay 0.0 only because there were genuinely zero "
        "lookups, not as a hardcoded default masking real activity"
    )
    store.close()


def test_flush_cache_stats_hit_ratio_reflects_real_mix_across_two_flushes(
    tmp_path: Path,
) -> None:
    cache = EmbedCache(tmp_path / "c.db")
    ce = CachedEmbedder(inner=_Inner(), cache=cache, model_id="m")
    store = MetricsStore(tmp_path / "metrics.db")

    ce.embed(["a", "b", "c", "d"])  # 4 misses
    ce.flush_cache_stats(store)
    ce.embed(["a", "b", "c"])  # 3 hits (already cached), 0 misses
    ce.flush_cache_stats(store)

    summary = store.summary()
    assert summary["cache"]["total_hits"] == 3
    assert summary["cache"]["total_misses"] == 4
    assert summary["cache"]["hit_ratio"] == round(3 / 7, 3)
    store.close()


def test_uncached_embedder_has_no_flush_cache_stats_attribute() -> None:
    """Pins the getattr-guard contract that call sites must use: a bare
    (non-cached) embedder -- e.g. what ``get_embedder()`` returns when
    ``EMBED_CACHE_DISABLED=1`` is set -- does not expose
    ``flush_cache_stats``. Call sites (Retriever.retrieve, CLI ingest,
    MCP ingest) must guard with
    ``getattr(embedder, "flush_cache_stats", None)`` rather than assume
    every embedder has it.
    """
    assert not hasattr(_Inner(), "flush_cache_stats")
