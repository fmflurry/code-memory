"""Autostart adapters: dispatch + unit generation (no real service start)."""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

from code_memory.sync.autostart.base import get_adapter, watcher_command


def test_get_adapter_matches_current_platform() -> None:
    system = platform.system()
    adapter = get_adapter()
    name = type(adapter).__name__
    if system == "Darwin":
        assert name == "LaunchdAdapter"
    elif system == "Linux":
        assert name == "SystemdUserAdapter"
    elif system == "Windows":
        assert name == "SchtasksAdapter"


def test_watcher_command_returns_executable_and_args(tmp_path: Path) -> None:
    cmd = watcher_command(tmp_path)
    assert cmd[-2] == "watch"
    assert cmd[-1] == str(tmp_path)


@pytest.mark.skipif(platform.system() != "Darwin", reason="launchd only on macOS")
def test_launchd_install_writes_plist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from code_memory.sync.autostart.launchd import LaunchdAdapter

    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    adapter = LaunchdAdapter()
    repo = tmp_path / "repo"
    repo.mkdir()
    status = adapter.install(repo)
    assert status.installed
    assert status.unit_path is not None
    plist = Path(status.unit_path)
    assert plist.is_file()
    content = plist.read_bytes()
    assert b"com.codememory.watch" in content
    assert str(repo).encode() in content


@pytest.mark.skipif(platform.system() != "Linux", reason="systemd only on Linux")
def test_systemd_install_writes_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from code_memory.sync.autostart.systemd import SystemdUserAdapter

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    adapter = SystemdUserAdapter()
    repo = tmp_path / "repo"
    repo.mkdir()
    status = adapter.install(repo)
    assert status.installed
    assert status.unit_path is not None
    content = Path(status.unit_path).read_text()
    assert "[Service]" in content
    assert "Restart=on-failure" in content
    assert str(repo) in content
