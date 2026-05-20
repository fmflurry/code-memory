"""Tests for git_delta. Spins up a real local git repo in tmp_path."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from code_memory.orchestrator import git_delta


def _has_git() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not on PATH")


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def test_is_git_repo(tmp_path: Path) -> None:
    assert not git_delta.is_git_repo(tmp_path)
    _init_repo(tmp_path)
    (tmp_path / "f.py").write_text("x = 1\n")
    _commit(tmp_path, "init")
    assert git_delta.is_git_repo(tmp_path)


def test_head_and_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("a = 1\n")
    sha = _commit(tmp_path, "first")
    assert git_delta.head_sha(tmp_path) == sha
    assert git_delta.current_branch(tmp_path) == "main"


def test_is_reachable(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("a = 1\n")
    sha = _commit(tmp_path, "first")
    assert git_delta.is_reachable(tmp_path, sha)
    assert not git_delta.is_reachable(tmp_path, "0" * 40)


def test_diff_modify_add_delete(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "keep.py").write_text("k = 1\n")
    (tmp_path / "gone.py").write_text("g = 1\n")
    (tmp_path / "edit.py").write_text("e = 1\n")
    base = _commit(tmp_path, "base")

    (tmp_path / "gone.py").unlink()
    (tmp_path / "edit.py").write_text("e = 2\n")
    (tmp_path / "new.py").write_text("n = 1\n")
    _commit(tmp_path, "next")

    d = git_delta.diff(tmp_path, base, "HEAD")
    changed = {p.name for p in d.changed}
    deleted = {p.name for p in d.deleted}
    assert "new.py" in changed
    assert "edit.py" in changed
    assert "gone.py" in deleted
    assert "keep.py" not in changed
    assert "keep.py" not in deleted


def test_diff_rename(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "old_name.py").write_text("def foo():\n    return 1\n" * 5)
    base = _commit(tmp_path, "base")

    src = (tmp_path / "old_name.py").read_text()
    (tmp_path / "old_name.py").unlink()
    (tmp_path / "new_name.py").write_text(src)
    _commit(tmp_path, "rename")

    d = git_delta.diff(tmp_path, base, "HEAD")
    changed = {p.name for p in d.changed}
    deleted = {p.name for p in d.deleted}
    # Rename detection: old goes to deleted, new to changed
    assert "new_name.py" in changed
    assert "old_name.py" in deleted


def test_dirty_files(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "tracked.py").write_text("t = 1\n")
    _commit(tmp_path, "init")

    (tmp_path / "tracked.py").write_text("t = 2\n")
    (tmp_path / "untracked.py").write_text("u = 1\n")

    dirty = {p.name for p in git_delta.dirty_files(tmp_path)}
    assert "tracked.py" in dirty
    assert "untracked.py" in dirty


def test_reingest_paths_dedups(tmp_path: Path) -> None:
    d = git_delta.Delta(
        changed=[tmp_path / "a", tmp_path / "b"],
        deleted=[],
        dirty=[tmp_path / "b", tmp_path / "c"],
    )
    names = [p.name for p in d.reingest_paths()]
    assert names == ["a", "b", "c"]


def test_changed_since_includes_dirty(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("a = 1\n")
    base = _commit(tmp_path, "base")
    (tmp_path / "b.py").write_text("b = 1\n")
    _commit(tmp_path, "next")
    (tmp_path / "c.py").write_text("c = 1\n")  # uncommitted

    d = git_delta.changed_since(tmp_path, base, include_dirty=True)
    names = {p.name for p in d.reingest_paths()}
    assert {"b.py", "c.py"}.issubset(names)
