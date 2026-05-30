"""Guards against watching HOME or filesystem roots."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_memory.sync.safety import (
    UnsafeWatchRootError,
    assert_safe_watch_root,
    is_ephemeral_watch_dir,
)


def test_assert_safe_rejects_home() -> None:
    with pytest.raises(UnsafeWatchRootError):
        assert_safe_watch_root(Path.home())


def test_assert_safe_rejects_filesystem_root() -> None:
    with pytest.raises(UnsafeWatchRootError):
        assert_safe_watch_root("/")


def test_assert_safe_rejects_tmp() -> None:
    with pytest.raises(UnsafeWatchRootError):
        assert_safe_watch_root("/tmp")


def test_assert_safe_accepts_project_dir(tmp_path: Path) -> None:
    repo = tmp_path / "my-repo"
    repo.mkdir()
    resolved = assert_safe_watch_root(repo)
    assert resolved == repo.resolve()


def test_assert_safe_expands_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a per-test HOME so ``~/...`` expansion doesn't collide
    # with the user's actual home directory.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    repo = fake_home / "repo"
    repo.mkdir()
    resolved = assert_safe_watch_root("~/repo")
    assert resolved == repo.resolve()


def test_assert_safe_rejects_resolved_home_via_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # symlink target = HOME → must still be rejected after resolve()
    link = tmp_path / "linked-home"
    link.symlink_to(fake_home)
    with pytest.raises(UnsafeWatchRootError):
        assert_safe_watch_root(link)


def test_ensure_autostart_returns_unsafe_status_for_home() -> None:
    from code_memory.sync.autostart.base import ensure_autostart

    st = ensure_autostart(Path.home())
    assert not st.installed
    assert not st.running
    assert st.label == "<unsafe-root>"
    assert "refusing to watch" in (st.note or "")


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_is_ephemeral_detects_homunculus_session_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    d = home / ".claude" / "homunculus" / "projects" / "65e843763e3b"
    d.mkdir(parents=True)
    assert is_ephemeral_watch_dir(d) is True


def test_is_ephemeral_detects_cursor_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    d = home / ".cursor" / "worktrees" / "gc.webapp" / "1v64"
    d.mkdir(parents=True)
    assert is_ephemeral_watch_dir(d) is True


def test_is_ephemeral_detects_plugin_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    d = home / ".claude" / "plugins" / "cache" / "code-memory" / "0.3.0"
    d.mkdir(parents=True)
    assert is_ephemeral_watch_dir(d) is True


def test_is_ephemeral_false_for_real_project(tmp_path: Path) -> None:
    repo = tmp_path / "Workspace" / "my-repo"
    repo.mkdir(parents=True)
    assert is_ephemeral_watch_dir(repo) is False


def test_ensure_autostart_skips_ephemeral_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.sync.autostart.base import ensure_autostart

    home = _fake_home(tmp_path, monkeypatch)
    d = home / ".claude" / "homunculus" / "projects" / "abc123"
    d.mkdir(parents=True)
    st = ensure_autostart(d)
    assert not st.installed
    assert not st.running
    assert st.label == "<ephemeral>"
    assert "ephemeral" in (st.note or "")
