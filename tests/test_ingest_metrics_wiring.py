"""Pins the fix for: CLI/MCP ingests complete successfully but are never
recorded in the metrics DB (``codememory_health`` always reports
``ingest.count == 0`` / ``last_ingest_ts: null`` no matter how many real
ingests have run).

Root cause (see diagnosis returned to orchestrator): ``MetricsStore.
record_ingest`` (src/code_memory/metrics.py) is defined but has **zero**
call sites anywhere in the source tree — neither the CLI ``ingest``
command (src/code_memory/cli.py::ingest) nor the MCP ``codememory_ingest``
handler (src/code_memory/mcp_server.py::_ingest) ever calls it. This is a
single missing-wiring bug that affects both entry points identically; it
is NOT a slug-normalisation bug, a swallowed exception, or two DBs
disagreeing on path — none of those apply because no write is ever
attempted in the first place.

A secondary, closely-related gap this file also pins: ``MetricsStore.
summary()`` has no ``project`` filter for the ``ingest`` block (unlike
``tool_usage_summary``/``efficiency_summary``, which already accept an
optional ``project`` kwarg). Once ``record_ingest`` is wired up, a
multi-project deployment would still report globally-merged ingest
counts under every project's health check unless ``summary()`` gains the
same per-project filtering convention. Tests below pin ``summary(project=
...)`` accepting and honouring that filter, explicitly using the
dot-form slug ("gc.webapp") the user passed end-to-end, so a future
dot/dash normalisation mismatch between the write side and the read side
would be caught here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from code_memory.cli import app
from code_memory.metrics import MetricsStore
from code_memory.orchestrator.pipeline import IngestStats


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    return repo


# ---------------------------------------------------------------------------
# CLI path — completed ingests must record exactly one row.
# ---------------------------------------------------------------------------


def test_completed_full_cli_ingest_records_ingest_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful `code-memory ingest --full` must add one row to
    ``ingest_stats`` with the real symbol count and a non-null timestamp."""
    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))

    fake_stats = IngestStats(files=10, symbols=42, mode="full")
    with patch("code_memory.cli.Pipeline.ingest_repo", return_value=fake_stats):
        runner = CliRunner()
        result = runner.invoke(app, ["ingest", str(repo), "--full", "--json"])

    assert result.exit_code == 0, result.output

    store = MetricsStore(metrics_db)
    summary = store.summary()
    assert summary["ingest"]["count"] == 1
    assert summary["ingest"]["total_symbols"] == 42
    assert summary["ingest"]["last_ingest_ts"] is not None
    store.close()


def test_completed_delta_cli_ingest_records_ingest_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful incremental (delta) ingest must also record — the bug
    is not specific to `--full`."""
    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))

    fake_stats = IngestStats(files=3, symbols=7, mode="incremental")
    with patch("code_memory.cli.Pipeline.ingest_repo", return_value=fake_stats):
        runner = CliRunner()
        result = runner.invoke(app, ["ingest", str(repo), "--json"])

    assert result.exit_code == 0, result.output

    store = MetricsStore(metrics_db)
    summary = store.summary()
    assert summary["ingest"]["count"] == 1
    assert summary["ingest"]["total_symbols"] == 7
    store.close()


# ---------------------------------------------------------------------------
# Project-slug round trip — the exact scenario the user hit: ingest with
# `--project gc.webapp` (dot form), then read back through the same slug.
# Includes a negative control: a health check for an unrelated project
# must NOT see this ingest, proving the filter is real and not a global
# count that happens to be nonzero.
# ---------------------------------------------------------------------------


def test_ingest_recorded_under_dot_form_slug_is_readable_by_same_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))

    fake_stats = IngestStats(files=5165, symbols=99, mode="full")
    with patch("code_memory.cli.Pipeline.ingest_repo", return_value=fake_stats):
        runner = CliRunner()
        result = runner.invoke(
            app, ["ingest", str(repo), "--project", "gc.webapp", "--full", "--json"]
        )

    assert result.exit_code == 0, result.output

    store = MetricsStore(metrics_db)
    same_slug = store.summary(project="gc.webapp")
    assert same_slug["ingest"]["count"] == 1
    assert same_slug["ingest"]["total_symbols"] == 99

    other_slug = store.summary(project="some-other-project")
    assert other_slug["ingest"]["count"] == 0, (
        "health for an unrelated project must not see gc.webapp's ingest — "
        "the ingest block must filter by project, not report a global count"
    )
    store.close()


# ---------------------------------------------------------------------------
# total_symbols must reflect the real count, never a hardcoded/default 0.
# ---------------------------------------------------------------------------


def test_total_symbols_reflects_actual_symbol_count_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))

    fake_stats = IngestStats(files=1, symbols=14442, mode="full")
    with patch("code_memory.cli.Pipeline.ingest_repo", return_value=fake_stats):
        runner = CliRunner()
        result = runner.invoke(app, ["ingest", str(repo), "--full", "--json"])

    assert result.exit_code == 0, result.output

    store = MetricsStore(metrics_db)
    assert store.summary()["ingest"]["total_symbols"] == 14442
    store.close()


# ---------------------------------------------------------------------------
# dry-run must NOT record (docstring: "compute plan ... don't touch
# storage"). NOTE: against the current broken code this assertion holds
# vacuously (nothing ever records), so it is not RED today — it exists to
# pin the intended semantics so a future fix can't regress it by recording
# unconditionally.
# ---------------------------------------------------------------------------


def test_dry_run_cli_ingest_does_not_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))

    fake_stats = IngestStats(files=10, symbols=42, mode="full")
    with patch("code_memory.cli.Pipeline.ingest_repo", return_value=fake_stats):
        runner = CliRunner()
        result = runner.invoke(
            app, ["ingest", str(repo), "--full", "--dry-run", "--json"]
        )

    assert result.exit_code == 0, result.output

    if metrics_db.exists():
        store = MetricsStore(metrics_db)
        assert store.summary()["ingest"]["count"] == 0
        store.close()


# ---------------------------------------------------------------------------
# A failed/aborted ingest must not leave a garbage row. Same caveat as the
# dry-run test: vacuously true today, pins intent for after the fix lands.
# ---------------------------------------------------------------------------


def test_failed_cli_ingest_does_not_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))

    with patch(
        "code_memory.cli.Pipeline.ingest_repo",
        side_effect=RuntimeError("boom: extractor crashed"),
    ):
        runner = CliRunner()
        result = runner.invoke(app, ["ingest", str(repo), "--full", "--json"])

    assert result.exit_code != 0

    if metrics_db.exists():
        store = MetricsStore(metrics_db)
        assert store.summary()["ingest"]["count"] == 0, (
            "a raised exception mid-ingest must not produce a recorded row"
        )
        store.close()


# ---------------------------------------------------------------------------
# MCP path — separate wiring from the CLI. A fix applied only to cli.py
# must not leave codememory_ingest still silently dropping metrics.
# ---------------------------------------------------------------------------


def test_completed_mcp_ingest_records_ingest_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import mcp_server

    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))
    monkeypatch.setenv("CODE_MEMORY_MCP_INGEST_ENABLED", "1")

    fake_stats = IngestStats(files=8, symbols=23, mode="full")
    with patch(
        "code_memory.mcp_server.Pipeline.ingest_repo", return_value=fake_stats
    ):
        mcp_server._ingest(
            {
                "project": "gc.webapp",
                "root": str(repo),
                "full": True,
                "confirmed": True,
            }
        )

    store = MetricsStore(metrics_db)
    summary = store.summary(project="gc.webapp")
    assert summary["ingest"]["count"] == 1
    assert summary["ingest"]["total_symbols"] == 23
    store.close()


def test_dry_run_mcp_ingest_does_not_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import mcp_server

    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))
    monkeypatch.setenv("CODE_MEMORY_MCP_INGEST_ENABLED", "1")

    fake_stats = IngestStats(files=8, symbols=23, mode="full")
    with patch(
        "code_memory.mcp_server.Pipeline.ingest_repo", return_value=fake_stats
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

    if metrics_db.exists():
        store = MetricsStore(metrics_db)
        assert store.summary()["ingest"]["count"] == 0
        store.close()


def test_mcp_ingest_disabled_path_never_touches_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When CODE_MEMORY_MCP_INGEST_ENABLED is unset (the default), the
    handler short-circuits to `status: disabled_use_cli` and never calls
    Pipeline.ingest_repo at all. This must not touch the metrics DB —
    guards against a fix landing on the wrong branch of `_ingest`."""
    from code_memory import mcp_server

    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))
    monkeypatch.delenv("CODE_MEMORY_MCP_INGEST_ENABLED", raising=False)

    mcp_server._ingest({"project": "gc.webapp", "root": str(repo), "full": True})

    assert not metrics_db.exists(), (
        "the disabled/preview response must not create or write the metrics DB"
    )


def test_mcp_ingest_confirmation_required_never_touches_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled but unconfirmed: handler returns `confirmation_required`
    without calling Pipeline.ingest_repo. Must not record."""
    from code_memory import mcp_server

    repo = _git_repo(tmp_path)
    metrics_db = tmp_path / "metrics.db"
    monkeypatch.setenv("CODEMEMORY_METRICS_DB", str(metrics_db))
    monkeypatch.setenv("CODE_MEMORY_MCP_INGEST_ENABLED", "1")

    mcp_server._ingest({"project": "gc.webapp", "root": str(repo), "full": True})

    assert not metrics_db.exists()
