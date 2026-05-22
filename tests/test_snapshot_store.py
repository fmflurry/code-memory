"""Snapshot store: orphan-branch git-backed blob storage."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from code_memory.sync.store import SnapshotStore


def _has_git() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not on PATH")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "f.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = SnapshotStore(repo)
    sha = "deadbeef" + "0" * 32
    blob = b"snapshot-payload-bytes"
    created = store.write(sha, blob, manifest={"head_sha": sha, "size": len(blob)}, push=False)
    assert created is True
    assert store.has(sha)
    assert store.read(sha) == blob


def test_idempotent_write(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = SnapshotStore(repo)
    sha = "a" * 40
    blob = b"abc"
    assert store.write(sha, blob, push=False) is True
    # Same content -> no-op
    assert store.write(sha, blob, push=False) is False


def test_list_local_reflects_writes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = SnapshotStore(repo)
    store.write("1" * 40, b"one", push=False)
    store.write("2" * 40, b"two", push=False)
    entries = {e.sha for e in store.list_local()}
    assert entries == {"1" * 40, "2" * 40}


def test_gc_keeps_recent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = SnapshotStore(repo)
    import time

    shas = [chr(ord("a") + i) * 40 for i in range(5)]
    for i, sha in enumerate(shas):
        store.write(sha, f"blob-{i}".encode(), manifest=None, push=False)
        time.sleep(0.001)
    removed = store.gc(keep_last=2, push=False)
    assert removed == 3
    remaining = {e.sha for e in store.list_local()}
    assert len(remaining) == 2
