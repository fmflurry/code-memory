"""Watcher routes git ref changes through the debouncer."""

from __future__ import annotations

import threading
from pathlib import Path

from code_memory.sync.watcher import Watcher


def _make_watcher(repo: Path) -> tuple[Watcher, dict[str, int]]:
    counts = {"n": 0}
    w = Watcher(repo, debounce=10.0)

    def fake_bump() -> None:
        counts["n"] += 1

    w._debouncer.bump = fake_bump  # type: ignore[method-assign]
    return w, counts


def test_git_head_change_triggers_bump(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    w, counts = _make_watcher(tmp_path)
    w._handle_path(tmp_path / ".git" / "HEAD")

    assert counts["n"] == 1


def test_branch_ref_change_triggers_bump(tmp_path: Path) -> None:
    refs_dir = tmp_path / ".git" / "refs" / "heads"
    refs_dir.mkdir(parents=True)
    ref = refs_dir / "feature"
    ref.write_text("abc123\n")

    w, counts = _make_watcher(tmp_path)
    w._handle_path(ref)

    assert counts["n"] == 1


def test_other_git_internals_do_not_trigger_bump(tmp_path: Path) -> None:
    objects = tmp_path / ".git" / "objects" / "ab"
    objects.mkdir(parents=True)
    blob = objects / "cdef"
    blob.write_text("blob")

    w, counts = _make_watcher(tmp_path)
    w._handle_path(blob)
    w._handle_path(tmp_path / ".git" / "index")

    assert counts["n"] == 0


def test_repo_file_triggers_bump(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    src = tmp_path / "src.py"
    src.write_text("x = 1")

    w, counts = _make_watcher(tmp_path)
    w._handle_path(src)

    assert counts["n"] == 1


def test_excluded_dir_does_not_trigger_bump(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    f = venv / "x.py"
    f.write_text("")

    w, counts = _make_watcher(tmp_path)
    w._handle_path(f)

    assert counts["n"] == 0


def test_handle_path_is_thread_safe(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    w, counts = _make_watcher(tmp_path)

    def hammer() -> None:
        for _ in range(50):
            w._handle_path(tmp_path / ".git" / "HEAD")

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert counts["n"] == 200
