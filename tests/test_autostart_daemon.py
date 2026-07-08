"""Phase 3 RED tests: collapse per-repo autostart units into ONE fixed
daemon unit (``com.codememory.watchd`` / ``codememory-watchd.service`` /
``CodeMemory\\Watchd``), driven by the on-disk watch registry, while
keeping a legacy sweep that prunes leftover per-repo units from older
code-memory versions.

These tests are intentionally RED — no implementation yet. They pin the
exact contract the ``coder`` agent must implement:

* ``watcher_command()`` (no ``repo`` arg) -> daemon command line ending in
  ``"watchd"``. The legacy ``watcher_command(repo)`` per-repo call
  continues to work unchanged (existing callers / tests depend on it).
* Each adapter (launchd / systemd / schtasks) gains
  ``install_daemon()``, ``start_daemon()``, ``status_daemon()``,
  ``uninstall_daemon()`` operating on ONE fixed unit identity, with NO
  per-repo working directory.
* ``ensure_autostart(repo)`` keeps its existing safety gates unchanged,
  then does ``registry.add(repo, slug)`` and ensures the single daemon
  unit is installed + running (idempotent across repos).
* ``prune_stale_autostart()`` still sweeps legacy per-repo units AND now
  also calls ``registry.prune()``.
* ``systemd``/``schtasks`` adapters gain their own ``prune_stale()`` for
  the legacy sweep (only ``launchd`` had one before this phase).

Scope: autostart adapter layer + ``base.py`` only. Does not touch
``cli.py`` (CLI migrate/status are a separate phase).
"""

from __future__ import annotations

import platform
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from code_memory.sync.autostart.base import (
    ensure_autostart,
    get_adapter,
    prune_stale_autostart,
    repo_label,
    watcher_command,
)

_DARWIN_ONLY = pytest.mark.skipif(
    platform.system() != "Darwin", reason="launchd only on macOS"
)
_LINUX_ONLY = pytest.mark.skipif(
    platform.system() != "Linux", reason="systemd only on Linux"
)
_WINDOWS_ONLY = pytest.mark.skipif(
    platform.system() != "Windows", reason="schtasks only on Windows"
)


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _stub_run(returncode: int = 0, stdout: str = "") -> Mock:
    """A ``subprocess.run`` stand-in returning a fixed fake CompletedProcess
    for every call, regardless of argv. Never touches a real service
    manager.
    """
    return Mock(return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr=""))


# ---------------------------------------------------------------------------
# 5. Adapter Protocol gains the daemon methods (structural check).
# ---------------------------------------------------------------------------


def test_adapter_exposes_daemon_lifecycle_methods() -> None:
    adapter = get_adapter()
    for name in ("install_daemon", "start_daemon", "status_daemon", "uninstall_daemon"):
        assert hasattr(adapter, name), f"adapter missing daemon method {name!r}"
        assert callable(getattr(adapter, name))


# ---------------------------------------------------------------------------
# 2. watcher_command(): daemon mode (no repo arg) -> ["watchd"] tail.
# ---------------------------------------------------------------------------


def test_watcher_command_no_repo_arg_returns_watchd() -> None:
    cmd = watcher_command()
    assert cmd[-1] == "watchd"
    assert "watch" not in cmd  # legacy per-repo subcommand token must be gone


def test_watcher_command_dev_fallback_uses_module_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_memory.sync.autostart.base as base_mod

    monkeypatch.setattr(base_mod.shutil, "which", lambda _name: None)
    cmd = watcher_command()
    assert cmd[0] == base_mod.sys.executable
    assert cmd[1:] == ["-m", "code_memory.cli", "watchd"]


def test_watcher_command_legacy_repo_arg_still_supported(tmp_path: Path) -> None:
    # Backward-compat: the legacy per-repo install/status/start paths (and
    # the prune_stale fixtures in test_autostart_adapters.py) still call
    # watcher_command(repo) and must keep working.
    cmd = watcher_command(tmp_path)
    assert cmd[-2] == "watch"
    assert cmd[-1] == str(tmp_path)


# ---------------------------------------------------------------------------
# 1 + 3. launchd: single fixed daemon plist, no per-repo WorkingDirectory.
# ---------------------------------------------------------------------------


@_DARWIN_ONLY
def test_launchd_install_daemon_writes_single_fixed_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import plistlib

    from code_memory.sync.autostart import launchd as launchd_mod

    home = _fake_home(tmp_path, monkeypatch)
    monkeypatch.setattr(launchd_mod.subprocess, "run", _stub_run(returncode=0))

    assert launchd_mod.DAEMON_LABEL == "com.codememory.watchd"

    adapter = launchd_mod.LaunchdAdapter()
    status = adapter.install_daemon()

    assert status.installed
    assert status.label == "com.codememory.watchd"
    plist_path = home / "Library" / "LaunchAgents" / "com.codememory.watchd.plist"
    assert plist_path.is_file()
    assert status.unit_path is not None
    assert Path(status.unit_path) == plist_path

    with plist_path.open("rb") as fh:
        data = plistlib.load(fh)

    assert data["Label"] == "com.codememory.watchd"
    assert data["ProgramArguments"][-1] == "watchd"
    assert "WorkingDirectory" not in data


@_DARWIN_ONLY
def test_launchd_uninstall_daemon_removes_fixed_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.sync.autostart import launchd as launchd_mod

    home = _fake_home(tmp_path, monkeypatch)
    monkeypatch.setattr(launchd_mod.subprocess, "run", _stub_run(returncode=0))

    adapter = launchd_mod.LaunchdAdapter()
    adapter.install_daemon()
    plist_path = home / "Library" / "LaunchAgents" / "com.codememory.watchd.plist"
    assert plist_path.is_file()

    status = adapter.uninstall_daemon()

    assert not status.installed
    assert not plist_path.is_file()


# ---------------------------------------------------------------------------
# 3. ensure_autostart: registry write + single-install idempotency +
#    no per-repo unit.
# ---------------------------------------------------------------------------


@_DARWIN_ONLY
def test_ensure_autostart_writes_registry_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_memory.sync.autostart.base as base_mod
    from code_memory.sync.autostart import launchd as launchd_mod

    _fake_home(tmp_path, monkeypatch)
    monkeypatch.setattr(launchd_mod.subprocess, "run", _stub_run(returncode=0))

    registry_add = Mock()
    monkeypatch.setattr(base_mod.registry, "add", registry_add)

    repo = tmp_path / "repo-a"
    repo.mkdir()

    base_mod.ensure_autostart(repo)

    assert registry_add.call_count == 1
    called_repo, called_slug = registry_add.call_args[0]
    assert Path(called_repo).resolve() == repo.resolve()
    assert called_slug == repo_label(repo)


@_DARWIN_ONLY
def test_ensure_autostart_installs_single_daemon_unit_once_across_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_memory.sync.autostart.base as base_mod
    from code_memory.sync.autostart import launchd as launchd_mod
    from code_memory.sync.autostart.launchd import LaunchdAdapter

    home = _fake_home(tmp_path, monkeypatch)

    daemon_running = {"value": False}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if argv[:2] == ["launchctl", "list"]:
            if daemon_running["value"]:
                stdout = f"1234\t0\t{launchd_mod.DAEMON_LABEL}\n"
            else:
                stdout = f"-\t0\t{launchd_mod.DAEMON_LABEL}\n"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if argv[:2] == ["launchctl", "kickstart"]:
            daemon_running["value"] = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(launchd_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(base_mod.registry, "add", Mock())

    install_calls: list[None] = []
    start_calls: list[None] = []
    orig_install = LaunchdAdapter.install_daemon
    orig_start = LaunchdAdapter.start_daemon

    def spy_install(self: LaunchdAdapter) -> object:
        install_calls.append(None)
        return orig_install(self)

    def spy_start(self: LaunchdAdapter) -> object:
        start_calls.append(None)
        return orig_start(self)

    monkeypatch.setattr(LaunchdAdapter, "install_daemon", spy_install)
    monkeypatch.setattr(LaunchdAdapter, "start_daemon", spy_start)

    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()

    base_mod.ensure_autostart(repo_a)
    base_mod.ensure_autostart(repo_b)

    assert len(install_calls) == 1, "daemon unit must be installed exactly once"
    assert len(start_calls) == 1, "daemon unit must be started exactly once"

    agents = home / "Library" / "LaunchAgents"
    daemon_plists = list(agents.glob("com.codememory.watchd.plist"))
    assert len(daemon_plists) == 1


@_DARWIN_ONLY
def test_ensure_autostart_does_not_create_per_repo_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_memory.sync.autostart.base as base_mod
    from code_memory.sync.autostart import launchd as launchd_mod

    home = _fake_home(tmp_path, monkeypatch)
    monkeypatch.setattr(launchd_mod.subprocess, "run", _stub_run(returncode=0))
    monkeypatch.setattr(base_mod.registry, "add", Mock())

    repo = tmp_path / "repo-a"
    repo.mkdir()

    base_mod.ensure_autostart(repo)

    agents = home / "Library" / "LaunchAgents"
    slug = repo_label(repo)
    per_repo_plist = agents / f"com.codememory.watch.{slug}.plist"
    assert not per_repo_plist.is_file(), "must not create a per-repo launchd unit"

    daemon_plist = agents / "com.codememory.watchd.plist"
    assert daemon_plist.is_file()


# ---------------------------------------------------------------------------
# 4. Legacy sweep retained + now also prunes the registry.
# ---------------------------------------------------------------------------


def test_prune_stale_autostart_also_prunes_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platform-neutral: stubs get_adapter() so this runs on any OS."""
    import code_memory.sync.autostart.base as base_mod

    class _StubAdapter:
        def prune_stale(self) -> list[str]:
            return ["com.codememory.watch.stale-repo"]

    monkeypatch.setattr(base_mod, "get_adapter", lambda: _StubAdapter())
    registry_prune = Mock()
    monkeypatch.setattr(base_mod.registry, "prune", registry_prune)

    removed = prune_stale_autostart()

    assert removed == ["com.codememory.watch.stale-repo"]
    assert registry_prune.call_count == 1


# ---------------------------------------------------------------------------
# 6. systemd: single unit identity + legacy sweep.
# ---------------------------------------------------------------------------


@_LINUX_ONLY
def test_systemd_install_daemon_writes_fixed_unit_without_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.sync.autostart import systemd as systemd_mod

    assert systemd_mod.DAEMON_UNIT == "codememory-watchd.service"

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(systemd_mod.subprocess, "run", _stub_run(returncode=0))

    adapter = systemd_mod.SystemdUserAdapter()
    status = adapter.install_daemon()

    assert status.installed
    assert status.label == "codememory-watchd.service"
    unit_path = Path(status.unit_path)
    assert unit_path.name == "codememory-watchd.service"
    content = unit_path.read_text()
    exec_start_line = next(
        line for line in content.splitlines() if line.startswith("ExecStart=")
    )
    assert exec_start_line.strip().endswith("watchd")
    assert "WorkingDirectory=" not in content


@_LINUX_ONLY
def test_systemd_prune_stale_removes_dead_legacy_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.sync.autostart import systemd as systemd_mod

    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(systemd_mod.subprocess, "run", _stub_run(returncode=0))

    units_dir = config_home / "systemd" / "user"
    units_dir.mkdir(parents=True)

    live_repo = tmp_path / "live-repo"
    live_repo.mkdir()
    dead_repo = tmp_path / "dead-repo"  # never created -> gone

    def _write_unit(name: str, workdir: Path) -> Path:
        unit_path = units_dir / name
        unit_path.write_text(
            "[Service]\n"
            f"ExecStart=/usr/bin/code-memory watch {workdir}\n"
            f"WorkingDirectory={workdir}\n"
        )
        return unit_path

    live_unit = _write_unit("codememory-watch-live-repo.service", live_repo)
    dead_unit = _write_unit("codememory-watch-dead-repo.service", dead_repo)

    adapter = systemd_mod.SystemdUserAdapter()
    removed = adapter.prune_stale()

    assert removed == ["codememory-watch-dead-repo.service"]
    assert live_unit.is_file()
    assert not dead_unit.is_file()


# ---------------------------------------------------------------------------
# 6. schtasks: single unit identity + legacy sweep.
# ---------------------------------------------------------------------------


@_WINDOWS_ONLY
def test_schtasks_daemon_identity_and_xml_has_no_repo_workdir() -> None:
    from code_memory.sync.autostart import schtasks as schtasks_mod

    adapter = schtasks_mod.SchtasksAdapter()
    assert adapter._daemon_task_name() == "CodeMemory\\Watchd"

    xml = schtasks_mod._daemon_task_xml()
    assert "watchd" in xml
    assert "<WorkingDirectory>" not in xml


@_WINDOWS_ONLY
def test_schtasks_install_daemon_calls_create_with_fixed_task_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_memory.sync.autostart import schtasks as schtasks_mod

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(schtasks_mod.subprocess, "run", fake_run)

    adapter = schtasks_mod.SchtasksAdapter()
    status = adapter.install_daemon()

    assert status.installed
    assert status.label == "CodeMemory\\Watchd"
    create_call = calls[0]
    assert "/Create" in create_call
    tn_idx = create_call.index("/TN")
    assert create_call[tn_idx + 1] == "CodeMemory\\Watchd"


@_WINDOWS_ONLY
def test_schtasks_prune_stale_removes_dead_legacy_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.sync.autostart import schtasks as schtasks_mod

    live_repo = tmp_path / "live-repo"
    live_repo.mkdir()
    dead_repo = tmp_path / "dead-repo"  # never created -> stays "gone"

    deleted: list[str] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if "/FO" in argv and "CSV" in argv:
            stdout = (
                '"TaskName"\r\n'
                '"\\CodeMemory\\Watch\\live-repo"\r\n'
                '"\\CodeMemory\\Watch\\dead-repo"\r\n'
            )
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if "/XML" in argv:
            tn_idx = argv.index("/TN")
            name = argv[tn_idx + 1]
            workdir = str(live_repo) if "live-repo" in name else str(dead_repo)
            xml = (
                "<Task><Actions><Exec>"
                f"<WorkingDirectory>{workdir}</WorkingDirectory>"
                "</Exec></Actions></Task>"
            )
            return SimpleNamespace(returncode=0, stdout=xml, stderr="")
        if "/Delete" in argv:
            tn_idx = argv.index("/TN")
            deleted.append(argv[tn_idx + 1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(schtasks_mod.subprocess, "run", fake_run)

    adapter = schtasks_mod.SchtasksAdapter()
    removed = adapter.prune_stale()

    assert removed == ["\\CodeMemory\\Watch\\dead-repo"]
    assert deleted == ["\\CodeMemory\\Watch\\dead-repo"]
