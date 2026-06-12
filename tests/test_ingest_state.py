"""Tests for the per-repo ingest_state SQLite store."""

from __future__ import annotations

from pathlib import Path

from code_memory.orchestrator.ingest_state import IngestState, IngestStateStore


def test_set_then_get(tmp_path: Path) -> None:
    store = IngestStateStore(tmp_path / "ep.db")
    repo = tmp_path / "repo"
    repo.mkdir()

    assert store.get(repo) is None

    store.set(repo, sha="abc123", branch="main")
    s = store.get(repo)
    assert s is not None
    assert s.last_sha == "abc123"
    assert s.branch == "main"
    assert s.last_ts > 0
    assert s.repo_root == str(repo.resolve())
    assert s.file_count is None
    assert s.symbol_count is None


def test_set_upserts(tmp_path: Path) -> None:
    store = IngestStateStore(tmp_path / "ep.db")
    repo = tmp_path / "repo"
    repo.mkdir()

    store.set(repo, sha="aaa", branch="main")
    store.set(repo, sha="bbb", branch="feature")
    s = store.get(repo)
    assert s is not None
    assert s.last_sha == "bbb"
    assert s.branch == "feature"


def test_clear(tmp_path: Path) -> None:
    store = IngestStateStore(tmp_path / "ep.db")
    repo = tmp_path / "repo"
    repo.mkdir()

    store.set(repo, sha="aaa")
    store.clear(repo)
    assert store.get(repo) is None


def test_two_repos_isolated(tmp_path: Path) -> None:
    store = IngestStateStore(tmp_path / "ep.db")
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    store.set(a, sha="aaa")
    store.set(b, sha="bbb")
    sa = store.get(a)
    sb = store.get(b)
    assert sa is not None and sb is not None
    assert sa.last_sha == "aaa"
    assert sb.last_sha == "bbb"


def test_set_and_get_with_counts(tmp_path: Path) -> None:
    store = IngestStateStore(tmp_path / "ep.db")
    repo = tmp_path / "repo"
    repo.mkdir()

    store.set(repo, sha="abc123", branch="main",
              file_count=100, symbol_count=500)
    s = store.get(repo)
    assert s is not None
    assert s.last_sha == "abc123"
    assert s.file_count == 100
    assert s.symbol_count == 500


def test_counts_updated_on_second_set(tmp_path: Path) -> None:
    store = IngestStateStore(tmp_path / "ep.db")
    repo = tmp_path / "repo"
    repo.mkdir()

    store.set(repo, sha="aaa", file_count=50, symbol_count=200)
    store.set(repo, sha="bbb", file_count=60, symbol_count=250)
    s = store.get(repo)
    assert s is not None
    assert s.last_sha == "bbb"
    assert s.file_count == 60
    assert s.symbol_count == 250


def test_counts_can_be_cleared_to_none(tmp_path: Path) -> None:
    store = IngestStateStore(tmp_path / "ep.db")
    repo = tmp_path / "repo"
    repo.mkdir()

    store.set(repo, sha="aaa", file_count=50, symbol_count=200)
    store.set(repo, sha="bbb")
    s = store.get(repo)
    assert s is not None
    # The ON CONFLICT DO UPDATE sets the columns to the new values
    # (None) since they aren't provided and default to None.
    assert s.file_count is None
    assert s.symbol_count is None


def test_backward_compat_legacy_db(tmp_path: Path) -> None:
    """Verify that a DB created without file_count/symbol_count columns
    still works after migration (columns added by ALTER TABLE)."""
    store = IngestStateStore(tmp_path / "legacy.db")
    repo = tmp_path / "repo"
    repo.mkdir()

    # Set with legacy call (no counts)
    store.set(repo, sha="old-skool", branch="legacy")
    s = store.get(repo)
    assert s is not None
    assert s.last_sha == "old-skool"
    assert s.file_count is None
    assert s.symbol_count is None

    # Now write with counts and verify they persist
    store.set(repo, sha="new-hotness", file_count=99, symbol_count=456)
    s = store.get(repo)
    assert s is not None
    assert s.last_sha == "new-hotness"
    assert s.file_count == 99
    assert s.symbol_count == 456
