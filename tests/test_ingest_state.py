"""Tests for the per-repo ingest_state SQLite store."""

from __future__ import annotations

from pathlib import Path

from code_memory.orchestrator.ingest_state import IngestStateStore


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
