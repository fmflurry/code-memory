import time
import sqlite3
from pathlib import Path

import pytest

from code_memory.metrics import MetricsStore, RetrieveTiming


def test_creates_db_file(tmp_path: Path):
    db_path = tmp_path / "metrics.db"
    store = MetricsStore(db_path)
    assert db_path.exists()
    store.close()


def test_record_retrieve_stores_and_recent(tmp_path: Path):
    db_path = tmp_path / "metrics.db"
    store = MetricsStore(db_path)

    timing = RetrieveTiming(
        query="test query",
        embed_ms=10.0,
        code_search_ms=20.0,
        eps_search_ms=5.0,
        claims_ms=2.0,
        total_ms=37.0,
        code_hit_count=3,
        eps_hit_count=1,
        claims_hit_count=0,
    )
    store.record_retrieve(timing)

    recent = store.recent_retrieves(limit=5)
    assert len(recent) == 1
    row = recent[0]
    assert row["query"] == "test query"
    assert row["embed_ms"] == 10.0
    assert row["total_ms"] == 37.0
    assert row["code_hit_count"] == 3
    store.close()


def test_record_backend_health(tmp_path: Path):
    db_path = tmp_path / "metrics.db"
    store = MetricsStore(db_path)

    store.record_backend_health("ollama", "healthy", 42.5)
    store.record_backend_health("qdrant", "degraded", 150.0)

    summary = store.summary()
    backends = summary["backends"]
    assert len(backends) == 2
    statuses = {b["backend"]: b["status"] for b in backends}
    assert statuses["ollama"] == "healthy"
    assert statuses["qdrant"] == "degraded"
    store.close()


def test_record_cache_stats(tmp_path: Path):
    db_path = tmp_path / "metrics.db"
    store = MetricsStore(db_path)

    store.record_cache_stats(hits=90, misses=10)
    store.record_cache_stats(hits=10, misses=0)

    summary = store.summary()
    assert summary["cache"]["total_hits"] == 100
    assert summary["cache"]["total_misses"] == 10
    expected_ratio = round(100 / 110, 3)
    assert summary["cache"]["hit_ratio"] == expected_ratio
    store.close()


def test_summary_zeros_when_empty(tmp_path: Path):
    db_path = tmp_path / "metrics.db"
    store = MetricsStore(db_path)

    summary = store.summary()

    assert summary["retrieves"]["count"] == 0
    assert summary["retrieves"]["avg_total_ms"] == 0.0
    assert summary["cache"]["total_hits"] == 0
    assert summary["cache"]["total_misses"] == 0
    assert summary["cache"]["hit_ratio"] == 0.0
    assert summary["ingest"]["count"] == 0
    assert summary["ingest"]["total_symbols"] == 0
    assert summary["backends"] == []
    store.close()


def test_summary_computes_avg_correctly(tmp_path: Path):
    db_path = tmp_path / "metrics.db"
    store = MetricsStore(db_path)

    for i in range(5):
        timing = RetrieveTiming(
            query=f"q{i}",
            embed_ms=5.0,
            code_search_ms=10.0,
            eps_search_ms=3.0,
            claims_ms=2.0,
            total_ms=20.0,
        )
        store.record_retrieve(timing)

    # one outlier
    timing = RetrieveTiming(
        query="slow",
        embed_ms=50.0,
        code_search_ms=100.0,
        eps_search_ms=30.0,
        claims_ms=20.0,
        total_ms=200.0,
    )
    store.record_retrieve(timing)

    summary = store.summary()
    assert summary["retrieves"]["count"] == 6
    expected_avg_total = (5 * 20.0 + 200.0) / 6
    assert summary["retrieves"]["avg_total_ms"] == round(expected_avg_total, 1)
    expected_avg_embed = (5 * 5.0 + 50.0) / 6
    assert summary["retrieves"]["avg_embed_ms"] == round(expected_avg_embed, 1)
    store.close()


def test_cleanup_tmp_path(tmp_path: Path):
    """Verify tmp_path is empty before use."""
    db_path = tmp_path / "metrics.db"
    assert not db_path.exists()
    store = MetricsStore(db_path)
    assert db_path.exists()
    store.close()
