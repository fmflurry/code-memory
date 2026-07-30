"""Pins FIX A's call-site wiring: the seam that actually invokes
``CachedEmbedder.flush_cache_stats`` (see test_embed_cache_flush.py for
the unit itself) from both long-lived (MCP server) and one-shot (CLI)
processes.

Seam decision, pinned by these tests: flush **once per unit of work**
-- once at the end of ``Retriever.retrieve()`` (mirrors the existing
non-fatal ``record_retrieve`` block in orchestrator/retrieve.py) and
once at the end of a completed ``ingest`` (mirrors the existing
``record_ingest`` block wired in cli.py / mcp_server.py in the prior
fix for ``record_ingest``, see test_ingest_metrics_wiring.py).

Rejected alternatives:
  * atexit / process-exit hook -- the MCP server is long-lived and may
    be killed rather than exit cleanly, so counters would never flush;
    also useless duplication for the CLI, which already flushes at
    the natural end of `retrieve`/`ingest`.
  * an explicit "flush" tool/command -- nobody would remember to call
    it; the whole point is that health should reflect real activity
    without an extra manual step.

Project-scoping decision, pinned by
``test_cache_stats_are_intentionally_global_not_project_scoped``: the
embed cache is a single global SQLite file shared across *all*
projects by design (embed/__init__.py:_cache_db_path -- "the same
content embedded twice across projects reuses the cached vector"), so
its hit/miss counters are correctly global too. This is different from
``ingest_stats``, which is genuinely per-project and already filters
correctly.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from code_memory.cli import app
from code_memory.embed import CachedEmbedder, EmbedCache
from code_memory.embed.m3 import HybridVec, SparseVec
from code_memory.metrics import MetricsStore
from code_memory.orchestrator.pipeline import IngestStats
from code_memory.orchestrator.retrieve import ENV_METRICS, Retriever


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    return repo


class _FakeVectorStore:
    """Retriever.vector double -- no live Qdrant needed for these tests."""

    def search(self, *args: object, **kwargs: object) -> list:
        return []


class _FakeEpisodicStore:
    """Retriever.episodic double -- no live SQLite episodic store needed."""

    def by_ids(self, ids: list[str]) -> list:
        return []


class _RecordingInner:
    """Deterministic inner embedder -- no live Ollama needed."""

    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        return [HybridVec(dense=[0.1], sparse=SparseVec([], [])) for _ in texts]

    def embed_one(self, text: str) -> HybridVec:
        return self.embed([text])[0]


def _retriever(tmp_path: Path, embedder: object, slug: str) -> Retriever:
    return Retriever(
        project=slug,
        embedder=embedder,  # type: ignore[arg-type]
        vector=_FakeVectorStore(),  # type: ignore[arg-type]
        episodic=_FakeEpisodicStore(),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Retriever.retrieve() must flush accumulated cache activity into MetricsStore.
# ---------------------------------------------------------------------------


def test_retrieve_flushes_a_cache_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_METRICS, str(tmp_path / "metrics.db"))
    cache = EmbedCache(tmp_path / "embed_cache.sqlite")
    embedder = CachedEmbedder(inner=_RecordingInner(), cache=cache, model_id="m")
    r = _retriever(tmp_path, embedder, "tdd-fixa-miss")

    r.retrieve("how does auth work")

    store = MetricsStore(tmp_path / "metrics.db")
    summary = store.summary()
    assert summary["cache"]["total_misses"] == 1
    assert summary["cache"]["total_hits"] == 0
    store.close()


def test_retrieve_flushes_a_hit_on_repeated_query_without_double_counting_the_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_METRICS, str(tmp_path / "metrics.db"))
    cache = EmbedCache(tmp_path / "embed_cache.sqlite")
    embedder = CachedEmbedder(inner=_RecordingInner(), cache=cache, model_id="m")
    r = _retriever(tmp_path, embedder, "tdd-fixa-hit")

    r.retrieve("how does auth work")  # miss, embedded + cached
    r.retrieve("how does auth work")  # same hash -> hit

    store = MetricsStore(tmp_path / "metrics.db")
    summary = store.summary()
    assert summary["cache"]["total_misses"] == 1
    assert summary["cache"]["total_hits"] == 1
    store.close()


def test_retrieve_does_not_double_count_cumulative_counters_across_many_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: EmbedCache.hits/.misses are cumulative-since-open.
    If the flush call site recorded the raw counters on every retrieve()
    instead of the delta-since-last-flush, three distinct-query calls
    would report 1 + 2 + 3 = 6 misses instead of 3."""
    monkeypatch.setenv(ENV_METRICS, str(tmp_path / "metrics.db"))
    cache = EmbedCache(tmp_path / "embed_cache.sqlite")
    embedder = CachedEmbedder(inner=_RecordingInner(), cache=cache, model_id="m")
    r = _retriever(tmp_path, embedder, "tdd-fixa-nodouble")

    for q in ["alpha query", "beta query", "gamma query"]:
        r.retrieve(q)

    store = MetricsStore(tmp_path / "metrics.db")
    assert store.summary()["cache"]["total_misses"] == 3
    store.close()


def test_retrieve_with_uncached_embedder_never_writes_cache_stats_and_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EMBED_CACHE_DISABLED=1 scenario: get_embedder() returns the bare
    inner embedder with no .cache / .flush_cache_stats. The call site
    must use a getattr guard, not assume the attribute exists."""
    monkeypatch.setenv(ENV_METRICS, str(tmp_path / "metrics.db"))
    r = _retriever(tmp_path, _RecordingInner(), "tdd-fixa-uncached")

    r.retrieve("plain embedder query")  # must not raise

    store = MetricsStore(tmp_path / "metrics.db")
    summary = store.summary()
    assert summary["cache"]["total_hits"] == 0
    assert summary["cache"]["total_misses"] == 0
    store.close()


# ---------------------------------------------------------------------------
# CLI `ingest` must flush cache stats after a completed ingest -- mirrors
# the record_ingest wiring pattern in test_ingest_metrics_wiring.py.
# ---------------------------------------------------------------------------


class _SpyCacheEmbedder:
    """Stands in for `pipe.embedder`. Performs a *real* MetricsStore write
    so the assertion proves the round trip through the same DB file, not
    merely "was called"."""

    def __init__(self, hits: int = 7, misses: int = 3) -> None:
        self.flush_calls: list[MetricsStore] = []
        self._hits = hits
        self._misses = misses

    def flush_cache_stats(self, metrics: MetricsStore) -> None:
        self.flush_calls.append(metrics)
        metrics.record_cache_stats(hits=self._hits, misses=self._misses)


def test_cli_ingest_flushes_cache_stats_after_completed_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))

    spy = _SpyCacheEmbedder(hits=7, misses=3)
    fake_stats = IngestStats(files=1, symbols=1, mode="full")
    with (
        patch("code_memory.orchestrator.pipeline.get_embedder", return_value=spy),
        patch("code_memory.cli.Pipeline.ingest_repo", return_value=fake_stats),
    ):
        runner = CliRunner()
        result = runner.invoke(app, ["ingest", str(repo), "--full", "--json"])

    assert result.exit_code == 0, result.output
    assert len(spy.flush_calls) == 1

    store = MetricsStore(metrics_db)
    summary = store.summary()
    assert summary["cache"]["total_hits"] == 7
    assert summary["cache"]["total_misses"] == 3
    store.close()


def test_cli_ingest_dry_run_does_not_flush_cache_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))

    spy = _SpyCacheEmbedder()
    fake_stats = IngestStats(files=1, symbols=1, mode="full")
    with (
        patch("code_memory.orchestrator.pipeline.get_embedder", return_value=spy),
        patch("code_memory.cli.Pipeline.ingest_repo", return_value=fake_stats),
    ):
        runner = CliRunner()
        result = runner.invoke(
            app, ["ingest", str(repo), "--full", "--dry-run", "--json"]
        )

    assert result.exit_code == 0, result.output
    assert spy.flush_calls == []


def test_cli_ingest_failure_does_not_flush_cache_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))

    spy = _SpyCacheEmbedder()
    with (
        patch("code_memory.orchestrator.pipeline.get_embedder", return_value=spy),
        patch(
            "code_memory.cli.Pipeline.ingest_repo",
            side_effect=RuntimeError("boom: extractor crashed"),
        ),
    ):
        runner = CliRunner()
        result = runner.invoke(app, ["ingest", str(repo), "--full", "--json"])

    assert result.exit_code != 0
    assert spy.flush_calls == []


# ---------------------------------------------------------------------------
# MCP `_ingest` -- separate wiring from the CLI. A fix landed only in
# cli.py must not leave codememory_ingest still silently dropping cache
# stats.
# ---------------------------------------------------------------------------


def test_mcp_ingest_flushes_cache_stats_after_completed_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import mcp_server

    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))
    monkeypatch.setenv("CODE_MEMORY_MCP_INGEST_ENABLED", "1")

    spy = _SpyCacheEmbedder(hits=5, misses=2)
    fake_stats = IngestStats(files=1, symbols=1, mode="full")
    with (
        patch("code_memory.orchestrator.pipeline.get_embedder", return_value=spy),
        patch(
            "code_memory.mcp_server.Pipeline.ingest_repo", return_value=fake_stats
        ),
    ):
        mcp_server._ingest(
            {
                "project": "gc.webapp",
                "root": str(repo),
                "full": True,
                "confirmed": True,
            }
        )

    assert len(spy.flush_calls) == 1
    store = MetricsStore(metrics_db)
    summary = store.summary(project="gc.webapp")
    assert summary["cache"]["total_hits"] == 5
    assert summary["cache"]["total_misses"] == 2
    store.close()


def test_mcp_ingest_dry_run_does_not_flush_cache_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import mcp_server

    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))
    monkeypatch.setenv("CODE_MEMORY_MCP_INGEST_ENABLED", "1")

    spy = _SpyCacheEmbedder()
    fake_stats = IngestStats(files=1, symbols=1, mode="full")
    with (
        patch("code_memory.orchestrator.pipeline.get_embedder", return_value=spy),
        patch(
            "code_memory.mcp_server.Pipeline.ingest_repo", return_value=fake_stats
        ),
    ):
        mcp_server._ingest(
            {
                "project": "gc.webapp",
                "root": str(repo),
                "full": True,
                "confirmed": True,
                "dry_run": True,
            }
        )

    assert spy.flush_calls == []


def test_mcp_ingest_disabled_path_never_touches_cache_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import mcp_server

    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))
    monkeypatch.delenv("CODE_MEMORY_MCP_INGEST_ENABLED", raising=False)

    spy = _SpyCacheEmbedder()
    with patch("code_memory.orchestrator.pipeline.get_embedder", return_value=spy):
        mcp_server._ingest({"project": "gc.webapp", "root": str(repo), "full": True})

    assert spy.flush_calls == []
    assert not metrics_db.exists()


# ---------------------------------------------------------------------------
# Project-scoping decision for cache counters: intentionally GLOBAL.
# ---------------------------------------------------------------------------


def test_cache_stats_are_intentionally_global_not_project_scoped(
    tmp_path: Path,
) -> None:
    """Unlike ingest_stats (per-project, see test_ingest_metrics_wiring.py),
    cache_stats has no project column and summary()'s cache block has no
    WHERE clause. This is deliberate: the embed cache is one global
    SQLite file shared by every project (see
    embed/__init__.py::_cache_db_path), so a lookup recorded while
    serving project A's retrieve/ingest is just as "real" cache activity
    for project B's health check. This test is a pinning test (already
    green today) guarding against a well-intentioned but wrong future
    change that adds a project filter here to "match" ingest_stats.
    """
    store = MetricsStore(tmp_path / "metrics.db")
    store.record_cache_stats(hits=10, misses=5)

    summary_a = store.summary(project="project-a")
    summary_b = store.summary(project="project-b")

    assert summary_a["cache"]["total_hits"] == 10 == summary_b["cache"]["total_hits"]
    assert summary_a["cache"]["total_misses"] == 5 == summary_b["cache"]["total_misses"]
    store.close()


# ---------------------------------------------------------------------------
# Read path: recorded stats must be visible through whatever
# `codememory_health` uses under the hood.
# ---------------------------------------------------------------------------


def test_health_surfaces_recorded_cache_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinning test for the read side (already correct today -- only the
    write side was disconnected). Confirms MetricsStore.summary()'s cache
    block, which _health already forwards verbatim, reflects real writes
    end to end."""
    from code_memory import mcp_server

    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))
    store = MetricsStore(metrics_db)
    store.record_cache_stats(hits=42, misses=8)
    store.close()

    result = mcp_server._health({"project": "tdd-fixa-health"})
    payload = json.loads(result[0].text)
    assert payload["metrics"]["cache"]["total_hits"] == 42
    assert payload["metrics"]["cache"]["total_misses"] == 8
    assert payload["metrics"]["cache"]["hit_ratio"] == round(42 / 50, 3)
