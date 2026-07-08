"""RED tests for Phase 6.2: wire MCP server boot to the registry+daemon
autostart model and stop double-watching the active repo.

``_bootstrap_repo`` (``code_memory.mcp_server``) currently always starts an
in-process ``Watcher`` for the active repo at step 3 (see the module
docstring comment "belt-and-suspenders"), regardless of whether the new
single-daemon (``watchd``) autostart model is already covering that repo.
Once Phase 3/4 land a real daemon, that means the SAME repo gets synced by
both the daemon and an in-process watcher — double work, and a potential
race between two independent debounced syncs.

This file pins the GREEN contract for the ``coder`` agent:

1. Opportunistic migrate (best-effort). ``_bootstrap_repo`` must attempt
   ``code_memory.cli.autostart_migrate`` (the Phase-4 verified-teardown
   entrypoint — seed -> install+start daemon -> VERIFY coverage -> only
   then remove legacy units) on every boot where autostart is enabled.
   The attempt must be wrapped so ANY exception raised by
   ``autostart_migrate`` is swallowed/logged and never propagates out of
   ``_bootstrap_repo`` — boot must always complete. Do NOT reimplement
   teardown logic here; only call the existing entrypoint.

2. No double in-process watcher when the daemon is confirmed running.
   ``_bootstrap_repo`` must consult
   ``code_memory.sync.autostart.base.get_adapter().status_daemon()``; when
   ``.running`` is True, the step-3 in-process ``Watcher`` must NOT be
   constructed/started.

3. In-process watcher fallback retained. When the daemon is NOT running
   (``status_daemon().running is False``) OR the running-check itself
   raises (adapter unavailable / unsupported OS / etc.), the in-process
   ``Watcher`` fallback IS started, exactly like today, so the active repo
   never goes uncovered. The running-check must be fail-safe: an
   exception is treated as "not running", never as "assume covered".

4. Registry registration is preserved. ``ensure_autostart(repo)`` (from
   ``code_memory.sync.autostart``) must still be invoked on every boot
   where autostart is enabled -- migration/daemon-gating is additive, not
   a replacement for registry registration.

5. Teardown/shutdown-hook wiring is preserved for the fallback watcher.
   ``_BOOTSTRAP_REFS["watcher"]`` must be populated if AND ONLY IF the
   step-3 fallback watcher actually started (i.e. it must track the real
   gating decision, not unconditionally hold a reference), and
   ``_teardown_watcher()`` must still cleanly stop + clear it.

None of this gating exists yet: today's ``_bootstrap_repo`` never calls
``autostart_migrate`` at all, and always starts the in-process ``Watcher``
whenever ``CODE_MEMORY_NO_INPROC_WATCHER`` is unset, with no daemon-status
check. Expected RED failure modes below are asserted explicitly (missing
migrate call / watcher started despite daemon running), not accidental
AttributeErrors from typos.

Scope: ``src/code_memory/mcp_server.py`` (``_bootstrap_repo`` and any new
private helpers it needs). Does not touch other phases' test files.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from code_memory import mcp_server
from code_memory.sync import autostart as autostart_pkg
from code_memory.sync.autostart import base as autostart_base_mod
import code_memory.cli as cli_mod
from code_memory.sync.autostart.base import AutostartStatus


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_bootstrap_refs():
    mcp_server._BOOTSTRAP_REFS.pop("watcher", None)
    yield
    mcp_server._BOOTSTRAP_REFS.pop("watcher", None)


def _prep_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Minimal fake git repo + env so ``_bootstrap_repo`` reaches step 3
    without touching real backends or the real filesystem watcher."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("CODE_MEMORY_REPO", str(tmp_path))
    monkeypatch.setenv("CODE_MEMORY_NO_HEALTH_CHECK", "1")
    monkeypatch.setenv("CODE_MEMORY_NO_BOOT_SYNC", "1")
    monkeypatch.delenv("CODE_MEMORY_NO_AUTOSTART", raising=False)
    monkeypatch.delenv("CODE_MEMORY_NO_INPROC_WATCHER", raising=False)
    return tmp_path.resolve()


def _patch_registry_noops(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Patch step-1 registry calls (source module: ``code_memory.sync.autostart``,
    the aggregator ``mcp_server`` imports from) to no-ops; returns the list
    of repos ``ensure_autostart`` was called with, for spying."""
    calls: list[Path] = []

    def _ensure_autostart(repo: Path, **_kw: Any) -> AutostartStatus:
        calls.append(repo)
        return AutostartStatus(installed=True, running=True, label="watchd")

    monkeypatch.setattr(autostart_pkg, "ensure_autostart", _ensure_autostart)
    monkeypatch.setattr(autostart_pkg, "prune_stale_autostart", lambda: [])
    return calls


def _patch_migrate(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    """Patch the Phase-4 migrate entrypoint at its source module
    (``code_memory.cli.autostart_migrate``), matching the local-import
    idiom already used elsewhere in this codebase (patch the source
    module attribute, not a name imported into ``mcp_server``'s
    namespace)."""
    monkeypatch.setattr(cli_mod, "autostart_migrate", fn)


def _patch_daemon_status(monkeypatch: pytest.MonkeyPatch, *, running: bool | None) -> None:
    """Patch ``get_adapter().status_daemon()`` at its source module.

    ``running=None`` makes ``status_daemon`` raise instead of returning,
    to exercise the fail-safe / tolerant path.
    """

    class _FakeAdapter:
        def status_daemon(self) -> AutostartStatus:
            if running is None:
                raise RuntimeError("adapter unavailable")
            return AutostartStatus(installed=True, running=running, label="watchd")

    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: _FakeAdapter())


def _patch_daemon_covers_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, watched_roots: list[str]
) -> None:
    """Point ``mcp_server.watchd_state_path()`` at a state file whose
    ``watched_roots`` includes the resolved repo, so
    ``_daemon_covers_repo`` (which supersedes the old process-alive-only
    ``_daemon_is_running`` check) reports coverage for this repo."""
    state_path = tmp_path / "watchd-state.json"
    state_path.write_text(
        json.dumps({"watched_roots": watched_roots}), encoding="utf-8"
    )
    monkeypatch.setattr(mcp_server, "watchd_state_path", lambda: state_path)


class _FakeWatcher:
    """Stand-in for ``code_memory.sync.watcher.Watcher`` that never touches
    the real filesystem/watchdog machinery."""

    instances: list["_FakeWatcher"] = []

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.stopped = 0
        self.started = 0
        _FakeWatcher.instances.append(self)

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


@pytest.fixture(autouse=True)
def _clean_fake_watcher_instances():
    _FakeWatcher.instances.clear()
    yield
    _FakeWatcher.instances.clear()


def _patch_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patched at the SOURCE module: mcp_server does
    # ``from .sync.watcher import Watcher`` as a local import inside
    # ``_bootstrap_repo``, so patching ``code_memory.sync.watcher.Watcher``
    # is observed on the next call.
    import code_memory.sync.watcher as watcher_mod

    monkeypatch.setattr(watcher_mod, "Watcher", _FakeWatcher)


# ---------------------------------------------------------------------------
# 1. Opportunistic migrate: best-effort, never blocks boot
# ---------------------------------------------------------------------------


def test_bootstrap_attempts_migrate_and_swallows_its_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _prep_repo(tmp_path, monkeypatch)
    _patch_registry_noops(monkeypatch)
    _patch_daemon_status(monkeypatch, running=True)
    _patch_watcher(monkeypatch)

    migrate_calls: list[Any] = []

    def _boom(*args: Any, **kwargs: Any) -> None:
        migrate_calls.append((args, kwargs))
        raise RuntimeError("migrate exploded")

    _patch_migrate(monkeypatch, _boom)

    # Must not raise -- boot completes even though migrate blew up.
    result = mcp_server._bootstrap_repo()

    assert migrate_calls, (
        "expected _bootstrap_repo to attempt code_memory.cli.autostart_migrate "
        "at least once during boot; it was never called"
    )
    assert result == repo


# ---------------------------------------------------------------------------
# 2 & 3. Daemon-running gate for the in-process watcher fallback
# ---------------------------------------------------------------------------


def test_bootstrap_does_not_start_inproc_watcher_when_daemon_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _prep_repo(tmp_path, monkeypatch)
    _patch_registry_noops(monkeypatch)
    _patch_migrate(monkeypatch, lambda *a, **kw: None)
    _patch_daemon_status(monkeypatch, running=True)
    _patch_daemon_covers_repo(monkeypatch, tmp_path, watched_roots=[str(repo)])
    _patch_watcher(monkeypatch)

    mcp_server._bootstrap_repo()

    assert _FakeWatcher.instances == [], (
        "in-process Watcher must NOT start when the daemon is confirmed "
        "running (double-watch the same repo)"
    )
    assert "watcher" not in mcp_server._BOOTSTRAP_REFS


def test_bootstrap_starts_inproc_watcher_fallback_when_daemon_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prep_repo(tmp_path, monkeypatch)
    _patch_registry_noops(monkeypatch)
    _patch_migrate(monkeypatch, lambda *a, **kw: None)
    _patch_daemon_status(monkeypatch, running=False)
    _patch_watcher(monkeypatch)

    mcp_server._bootstrap_repo()

    assert len(_FakeWatcher.instances) == 1, (
        "in-process Watcher fallback must start when the daemon is NOT "
        "running, so the active repo stays covered"
    )
    assert _FakeWatcher.instances[0].started == 1


def test_bootstrap_starts_inproc_watcher_fallback_when_daemon_check_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-safe: an exception from status_daemon() must be treated as
    "not running", never as "assume covered, skip the fallback"."""
    _prep_repo(tmp_path, monkeypatch)
    _patch_registry_noops(monkeypatch)
    _patch_migrate(monkeypatch, lambda *a, **kw: None)
    _patch_daemon_status(monkeypatch, running=None)  # raises
    _patch_watcher(monkeypatch)

    mcp_server._bootstrap_repo()

    assert len(_FakeWatcher.instances) == 1, (
        "a raising daemon-status check must fail SAFE (start the fallback "
        "watcher), not silently skip watcher coverage"
    )


# ---------------------------------------------------------------------------
# 4. Registry registration (ensure_autostart) still happens
# ---------------------------------------------------------------------------


def test_bootstrap_still_calls_ensure_autostart_before_migrate_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prep_repo(tmp_path, monkeypatch)
    ensure_calls = _patch_registry_noops(monkeypatch)
    _patch_daemon_status(monkeypatch, running=True)
    _patch_watcher(monkeypatch)

    migrate_calls: list[Any] = []
    _patch_migrate(monkeypatch, lambda *a, **kw: migrate_calls.append(1))

    mcp_server._bootstrap_repo()

    assert ensure_calls, "ensure_autostart(repo) must still be invoked during boot"
    assert migrate_calls, (
        "expected the opportunistic migrate attempt to also run this boot "
        "(registration and migration are additive, not exclusive)"
    )


# ---------------------------------------------------------------------------
# 5. Teardown/shutdown-hook wiring tracks the real gating decision
# ---------------------------------------------------------------------------


def test_bootstrap_refs_track_fallback_watcher_gating_and_teardown_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _prep_repo(tmp_path, monkeypatch)
    _patch_registry_noops(monkeypatch)
    _patch_migrate(monkeypatch, lambda *a, **kw: None)
    _patch_watcher(monkeypatch)

    # Case A: daemon running AND covering this repo -> no fallback
    # watcher -> nothing to leak.
    _patch_daemon_status(monkeypatch, running=True)
    _patch_daemon_covers_repo(monkeypatch, tmp_path, watched_roots=[str(repo)])
    mcp_server._bootstrap_repo()
    assert "watcher" not in mcp_server._BOOTSTRAP_REFS, (
        "no fallback watcher was started, so _BOOTSTRAP_REFS must not hold "
        "a stale/phantom watcher reference"
    )

    # Case B: daemon not running (and no longer covering this repo) ->
    # fallback watcher started and wired for graceful teardown via the
    # existing _teardown_watcher() path.
    _patch_daemon_status(monkeypatch, running=False)
    _patch_daemon_covers_repo(monkeypatch, tmp_path, watched_roots=[])
    mcp_server._bootstrap_repo()
    assert "watcher" in mcp_server._BOOTSTRAP_REFS, (
        "fallback watcher must be registered in _BOOTSTRAP_REFS so "
        "_teardown_watcher()/_install_shutdown_hooks() can clean it up"
    )
    fake_watcher = mcp_server._BOOTSTRAP_REFS["watcher"]

    mcp_server._teardown_watcher()

    assert fake_watcher.stopped == 1
    assert "watcher" not in mcp_server._BOOTSTRAP_REFS
