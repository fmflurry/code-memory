"""RED tests pinning FIXES to review-found bugs in the watchd migration /
MCP bootstrap wiring. These build on top of (but must not modify) the
existing test files:

  - tests/test_autostart_migrate.py            (Phase 4: `autostart migrate`)
  - tests/test_mcp_bootstrap_daemon_wiring.py   (Phase 6.2: MCP boot gating)
  - tests/test_autostart_daemon.py              (Phase 3: single watchd unit)

Fixes pinned here:

  1. CRITICAL -- ``autostart_migrate`` must seed/verify coverage from
     ``adapter.list_legacy_units()`` workdirs too, not solely the
     launchd-only ``registry.seed_from_units()``.
  2. CRITICAL -- the coverage check must not trivially pass
     (``set() <= watched_roots`` is always True for an empty LHS) when
     legacy units exist but nothing could be seeded.
  3. CRITICAL -- MCP bootstrap's in-process fallback Watcher gating must
     be coverage-aware for the ACTIVE repo (new helper
     ``code_memory.mcp_server._daemon_covers_repo(repo) -> bool``), not
     merely "is the daemon process alive".
  4. HIGH -- ``autostart_migrate`` must not force-restart an already
     healthy daemon when there is nothing to migrate.
  5. MEDIUM -- launchd's ``status_daemon().running`` must reflect an
     actual PID, not just "label is loaded" (``returncode == 0``).

None of this exists yet -- every test below is expected to fail against
the current implementation. See the accompanying GREEN spec (returned to
the orchestrator) for exact wiring the ``coder`` agent must implement.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from code_memory.cli import app
from code_memory.sync.autostart.base import AutostartStatus

runner = CliRunner()

_DARWIN_ONLY = pytest.mark.skipif(
    platform.system() != "Darwin", reason="launchd only on macOS"
)


# ---------------------------------------------------------------------------
# shared helpers (deliberately local to this file -- not imported from the
# other test files above, so this file stays independently reviewable)
# ---------------------------------------------------------------------------


def _fast_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the migrate verify-poll knobs so a verify-fail test never
    burns real wall-clock time waiting out the default timeout."""
    from code_memory import cli as cli_mod

    monkeypatch.setattr(cli_mod, "MIGRATE_VERIFY_TIMEOUT_S", 0.05, raising=False)
    monkeypatch.setattr(cli_mod, "MIGRATE_VERIFY_INTERVAL_S", 0.01, raising=False)


def _write_state(
    state_path: Path, *, pid: int, roots: list[str], ts: float = 1_700_000_000.0
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"pid": pid, "watched_roots": roots, "ts": ts}),
        encoding="utf-8",
    )


def _mock_adapter(
    legacy_units: list[dict[str, str | None]] | None = None,
    status_daemon: AutostartStatus | None = None,
) -> MagicMock:
    adapter = MagicMock()
    adapter.list_legacy_units.return_value = legacy_units or []
    if status_daemon is not None:
        adapter.status_daemon.return_value = status_daemon
    return adapter


# ---------------------------------------------------------------------------
# Fix 1 (CRITICAL): seed coverage from adapter.list_legacy_units() workdirs,
# not solely registry.seed_from_units() (launchd-only -- breaks on every
# other platform, and misses any legacy unit the registry glob doesn't
# know about).
# ---------------------------------------------------------------------------


def test_migrate_dry_run_seeded_roots_include_legacy_unit_workdirs_when_registry_seed_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Today ``autostart_migrate`` computes ``seeded_roots`` ONLY from
    ``registry.seed_from_units()`` (launchd-only, globs
    ``~/Library/LaunchAgents``). On non-macOS platforms -- or wherever
    ``seed_from_units`` legitimately returns ``[]`` -- the workdirs the
    adapter itself already discovered via ``list_legacy_units()`` are
    silently dropped from the seeded/covered-root set used by the verify
    step.

    Pin: ``seeded_roots``, as surfaced in the ``--dry-run --json`` payload
    (computed from the exact same set the real verify step consumes),
    must include every legacy unit's workdir even when
    ``registry.seed_from_units()`` contributes nothing.
    """
    from code_memory.sync import registry as registry_mod
    from code_memory.sync.autostart import base as autostart_base_mod

    monkeypatch.setattr(registry_mod, "seed_from_units", lambda: [])

    workdir_a = tmp_path / "legacy-repo-a"
    workdir_b = tmp_path / "legacy-repo-b"
    workdir_a.mkdir()
    workdir_b.mkdir()
    legacy_units = [
        {
            "label": "com.codememory.watch.legacy-repo-a",
            "unit_path": "/agents/a.plist",
            "workdir": str(workdir_a),
        },
        {
            "label": "com.codememory.watch.legacy-repo-b",
            "unit_path": "/agents/b.plist",
            "workdir": str(workdir_b),
        },
    ]
    adapter = _mock_adapter(legacy_units)
    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: adapter)

    result = runner.invoke(app, ["autostart", "migrate", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    seeded = set(payload.get("seeded_roots", []))
    assert str(workdir_a.resolve()) in seeded, (
        f"expected legacy unit workdir {workdir_a} to be included in "
        f"seeded_roots even though registry.seed_from_units() returned "
        f"[]; got seeded_roots={payload.get('seeded_roots')}"
    )
    assert str(workdir_b.resolve()) in seeded, (
        f"expected legacy unit workdir {workdir_b} to be included in "
        f"seeded_roots; got seeded_roots={payload.get('seeded_roots')}"
    )


def test_migrate_verify_requires_coverage_of_legacy_unit_workdirs_when_registry_seed_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end companion to the dry-run pin above: the REAL (non
    dry-run) verify step must also require coverage of the legacy units'
    workdirs, not just whatever (nothing, here) ``registry.seed_from_units``
    produced.

    ``watched_roots`` covers only ONE of the two legacy workdirs -> verify
    must fail (legacy retained, non-zero exit) because the merged seeded
    set the coder is required to compute is
    ``{workdir_a, workdir_b}``, and ``{workdir_a}`` is not a superset of
    that.
    """
    from code_memory import cli as cli_mod
    from code_memory.sync import registry as registry_mod
    from code_memory.sync.autostart import base as autostart_base_mod

    _fast_verify(monkeypatch)
    monkeypatch.setattr(registry_mod, "seed_from_units", lambda: [])

    workdir_a = tmp_path / "legacy-repo-a"
    workdir_b = tmp_path / "legacy-repo-b"
    workdir_a.mkdir()
    workdir_b.mkdir()
    legacy_units = [
        {
            "label": "com.codememory.watch.legacy-repo-a",
            "unit_path": "/agents/a.plist",
            "workdir": str(workdir_a),
        },
        {
            "label": "com.codememory.watch.legacy-repo-b",
            "unit_path": "/agents/b.plist",
            "workdir": str(workdir_b),
        },
    ]
    adapter = _mock_adapter(legacy_units)
    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: adapter)

    state_path = tmp_path / "watchd-state.json"
    _write_state(state_path, pid=os.getpid(), roots=[str(workdir_a.resolve())])
    monkeypatch.setattr(cli_mod, "watchd_state_path", lambda: state_path)

    result = runner.invoke(app, ["autostart", "migrate"])

    assert result.exit_code != 0, (
        f"expected verify-fail (workdir_b not covered) once legacy unit "
        f"workdirs feed the seeded set; got exit 0.\n{result.output}"
    )
    adapter.remove_legacy_unit.assert_not_called()
    lowered = result.output.lower()
    assert "incomplete" in lowered or "retained" in lowered, result.output


# ---------------------------------------------------------------------------
# Fix 2 (CRITICAL): empty-coverage guard -- ``set() <= watched_roots`` must
# not green-light teardown merely because nothing was (or could be)
# seeded, when legacy units still exist on disk.
# ---------------------------------------------------------------------------


def test_migrate_does_not_teardown_when_seeded_set_is_empty_but_legacy_units_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy units exist (there IS legacy state) but none of them carry a
    resolvable workdir (e.g. plist parsed fine but ``WorkingDirectory``
    was absent/stale) AND ``registry.seed_from_units()`` also returns
    ``[]`` -- so the merged seeded-root set is empty.
    ``set() <= watched_roots`` is trivially True for ANY ``watched_roots``
    (including an empty list), so a naive coverage check would treat this
    as "fully covered" and proceed to tear down the legacy units, even
    though nothing was ever verified to be watched.

    Pin: when ``legacy_units`` is non-empty but the seeded set is empty,
    migration must NOT proceed to teardown -- treat this as a verify-fail
    (legacy retained, non-zero exit), same as any other coverage failure.
    """
    from code_memory import cli as cli_mod
    from code_memory.sync import registry as registry_mod
    from code_memory.sync.autostart import base as autostart_base_mod

    _fast_verify(monkeypatch)
    monkeypatch.setattr(registry_mod, "seed_from_units", lambda: [])

    legacy_units = [
        {
            "label": "com.codememory.watch.dead-a",
            "unit_path": "/agents/dead-a.plist",
            "workdir": None,
        },
        {
            "label": "com.codememory.watch.dead-b",
            "unit_path": "/agents/dead-b.plist",
            "workdir": None,
        },
    ]
    adapter = _mock_adapter(legacy_units)
    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: adapter)

    state_path = tmp_path / "watchd-state.json"
    # Daemon is alive and trivially "covers" an empty root set.
    _write_state(state_path, pid=os.getpid(), roots=[])
    monkeypatch.setattr(cli_mod, "watchd_state_path", lambda: state_path)

    result = runner.invoke(app, ["autostart", "migrate"])

    assert result.exit_code != 0, (
        f"expected verify-fail when the seeded set is empty but legacy "
        f"units exist -- must not trivially pass coverage; got exit 0.\n"
        f"{result.output}"
    )
    adapter.remove_legacy_unit.assert_not_called()
    lowered = result.output.lower()
    assert "incomplete" in lowered or "retained" in lowered, result.output


# ---------------------------------------------------------------------------
# Fix 3 (CRITICAL): fallback Watcher gating must be coverage-aware for THIS
# repo, not merely "is the daemon process alive". Pins a new helper:
#
#     code_memory.mcp_server._daemon_covers_repo(repo: Path) -> bool
#
# See the GREEN spec for exact wiring into ``_bootstrap_repo`` (replaces
# ``_daemon_is_running`` at that call site).
# ---------------------------------------------------------------------------


def _write_mcp_state(
    state_path: Path, *, watched_roots: list[str], pid: int | None = None
) -> None:
    payload: dict[str, object] = {"watched_roots": watched_roots}
    if pid is not None:
        payload["pid"] = pid
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")


def test_daemon_covers_repo_true_when_repo_in_watched_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import mcp_server

    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = tmp_path / "watchd-state.json"
    _write_mcp_state(state_path, watched_roots=[str(repo.resolve())])
    monkeypatch.setattr(
        mcp_server, "watchd_state_path", lambda: state_path, raising=False
    )

    assert mcp_server._daemon_covers_repo(repo) is True


def test_daemon_covers_repo_false_when_repo_not_in_watched_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import mcp_server

    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other-repo"
    other.mkdir()
    state_path = tmp_path / "watchd-state.json"
    _write_mcp_state(state_path, watched_roots=[str(other.resolve())])
    monkeypatch.setattr(
        mcp_server, "watchd_state_path", lambda: state_path, raising=False
    )

    assert mcp_server._daemon_covers_repo(repo) is False


def test_daemon_covers_repo_false_when_state_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import mcp_server

    repo = tmp_path / "repo"
    repo.mkdir()
    missing_state = tmp_path / "does-not-exist" / "watchd-state.json"
    monkeypatch.setattr(
        mcp_server, "watchd_state_path", lambda: missing_state, raising=False
    )

    assert mcp_server._daemon_covers_repo(repo) is False, (
        "missing state file must fail SAFE (treated as 'not covered'), "
        "so the fallback watcher still starts"
    )


def test_daemon_covers_repo_false_when_state_file_unreadable_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import mcp_server

    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = tmp_path / "watchd-state.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(
        mcp_server, "watchd_state_path", lambda: state_path, raising=False
    )

    assert mcp_server._daemon_covers_repo(repo) is False


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


def _patch_registry_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_memory.sync import autostart as autostart_pkg

    monkeypatch.setattr(
        autostart_pkg,
        "ensure_autostart",
        lambda repo, **_kw: AutostartStatus(
            installed=True, running=True, label="watchd"
        ),
    )
    monkeypatch.setattr(autostart_pkg, "prune_stale_autostart", lambda: [])


def _patch_migrate_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    import code_memory.cli as cli_mod

    monkeypatch.setattr(cli_mod, "autostart_migrate", lambda *a, **kw: None)


def _patch_daemon_running(monkeypatch: pytest.MonkeyPatch, *, running: bool) -> None:
    from code_memory.sync.autostart import base as autostart_base_mod

    class _FakeAdapter:
        def status_daemon(self) -> AutostartStatus:
            return AutostartStatus(installed=True, running=running, label="watchd")

    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: _FakeAdapter())


class _FakeWatcher:
    """Stand-in for ``code_memory.sync.watcher.Watcher`` that never touches
    the real filesystem/watchdog machinery."""

    instances: list["_FakeWatcher"] = []

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.started = 0
        self.stopped = 0
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


@pytest.fixture(autouse=True)
def _clean_bootstrap_refs():
    from code_memory import mcp_server

    mcp_server._BOOTSTRAP_REFS.pop("watcher", None)
    yield
    mcp_server._BOOTSTRAP_REFS.pop("watcher", None)


def _patch_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    import code_memory.sync.watcher as watcher_mod

    monkeypatch.setattr(watcher_mod, "Watcher", _FakeWatcher)


def test_bootstrap_starts_fallback_when_daemon_running_but_repo_not_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 3, scenario 1: the daemon process IS running, but the
    resolved repo is NOT in ``watchd_state_path()``'s ``watched_roots``
    (e.g. a worktree/ephemeral dir ``ensure_autostart`` intentionally
    skipped, or a repo where ``ensure_autostart`` raised). The fallback
    Watcher must still start -- this is exactly the case the old
    process-alive-only ``_daemon_is_running`` check got wrong.
    """
    from code_memory import mcp_server

    repo = _prep_repo(tmp_path, monkeypatch)
    _patch_registry_noops(monkeypatch)
    _patch_migrate_noop(monkeypatch)
    _patch_daemon_running(monkeypatch, running=True)
    _patch_watcher(monkeypatch)

    state_path = tmp_path / "watchd-state.json"
    # Covers some OTHER repo, not this one.
    _write_mcp_state(state_path, watched_roots=[str(tmp_path / "some-other-repo")])
    monkeypatch.setattr(
        mcp_server, "watchd_state_path", lambda: state_path, raising=False
    )

    result = mcp_server._bootstrap_repo()

    assert result == repo
    assert len(_FakeWatcher.instances) == 1, (
        "daemon running but NOT covering this repo must still start the "
        "in-process fallback watcher"
    )
    assert _FakeWatcher.instances[0].started == 1
    assert "watcher" in mcp_server._BOOTSTRAP_REFS



# NOTE: "daemon running AND repo covered -> skip fallback watcher" is
# deliberately NOT pinned here as a standalone integration test: today's
# ``_daemon_is_running`` check already returns True whenever the daemon
# process is alive (regardless of coverage), so that scenario ALREADY
# passes against the current, unfixed implementation -- it isn't RED and
# adds no discriminating signal. The unit test
# ``test_daemon_covers_repo_true_when_repo_in_watched_roots`` above pins
# the "covered" half of the new helper; once ``_daemon_covers_repo``
# replaces ``_daemon_is_running`` in ``_bootstrap_repo`` the skip-when-
# covered behavior falls out for free and should be spot-checked as a
# regression during GREEN verification.


def test_bootstrap_starts_fallback_when_state_file_missing_failsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 3, scenario 3: a missing/unreadable state file is a
    fail-safe -> fallback watcher starts, even though the daemon process
    itself is reported running (e.g. ``ensure_autostart`` raised for this
    repo so it was never registered, or the daemon hasn't written state
    yet)."""
    from code_memory import mcp_server

    _prep_repo(tmp_path, monkeypatch)
    _patch_registry_noops(monkeypatch)
    _patch_migrate_noop(monkeypatch)
    _patch_daemon_running(monkeypatch, running=True)
    _patch_watcher(monkeypatch)

    missing_state = tmp_path / "does-not-exist" / "watchd-state.json"
    monkeypatch.setattr(
        mcp_server, "watchd_state_path", lambda: missing_state, raising=False
    )

    mcp_server._bootstrap_repo()

    assert len(_FakeWatcher.instances) == 1, (
        "missing/unreadable state file must fail SAFE (start the "
        "fallback watcher), not silently skip coverage"
    )


# ---------------------------------------------------------------------------
# Fix 4 (HIGH): migrate must not force-restart a healthy daemon when there
# is nothing to migrate (no legacy units at all).
# ---------------------------------------------------------------------------


def test_migrate_noop_when_nothing_to_migrate_and_daemon_already_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No legacy units exist AND the daemon already reports
    ``running=True`` -- there is nothing to seed and nothing new to
    verify. Pin: migrate must NOT call ``install_daemon()`` /
    ``start_daemon()`` (no kickstart of an already-healthy daemon) and
    must exit 0.
    """
    from code_memory.sync import registry as registry_mod
    from code_memory.sync.autostart import base as autostart_base_mod

    monkeypatch.setattr(registry_mod, "seed_from_units", lambda: [])

    adapter = _mock_adapter(
        legacy_units=[],
        status_daemon=AutostartStatus(installed=True, running=True, label="watchd"),
    )
    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: adapter)

    # Belt-and-suspenders determinism: even though the no-op fast path
    # should never reach the verify-poll at all, shrink the knobs and
    # point watchd_state_path at a guaranteed-missing file so that if the
    # fast path is NOT implemented, this test fails fast (in
    # MIGRATE_VERIFY_TIMEOUT_S) instead of burning the real 2s default.
    _fast_verify(monkeypatch)
    from code_memory import cli as cli_mod

    monkeypatch.setattr(
        cli_mod,
        "watchd_state_path",
        lambda: tmp_path / "does-not-exist" / "watchd-state.json",
    )

    result = runner.invoke(app, ["autostart", "migrate"])

    assert result.exit_code == 0, result.output
    adapter.install_daemon.assert_not_called()
    adapter.start_daemon.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 5 (MEDIUM): launchd status_daemon().running must reflect an actual
# PID, not merely "label is loaded" (returncode == 0).
# ---------------------------------------------------------------------------


def _fake_home_for_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> "object":
    from code_memory.sync.autostart import launchd as launchd_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    adapter = launchd_mod.LaunchdAdapter()
    plist_path = adapter._daemon_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(b"")  # presence is all `installed` checks for
    return adapter


@_DARWIN_ONLY
def test_launchd_status_daemon_not_running_when_launchctl_reports_dash_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.sync.autostart import launchd as launchd_mod

    adapter = _fake_home_for_daemon(tmp_path, monkeypatch)

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        assert argv[0] == "launchctl"
        assert argv[1] == "list"
        # Tabular `launchctl list` output: PID<TAB>Status<TAB>Label.
        # "-" in the PID column means loaded but not actually running.
        return SimpleNamespace(
            returncode=0, stdout="-\t0\tcom.codememory.watchd\n", stderr=""
        )

    monkeypatch.setattr(launchd_mod.subprocess, "run", fake_run)

    status = adapter.status_daemon()

    assert status.installed is True
    assert status.running is False, (
        "PID '-' means the job is loaded but not actually running; "
        "status_daemon().running must be False, not True-from-returncode"
    )


# NOTE: "numeric PID -> running True" is deliberately NOT pinned as a
# standalone test here: today's status_daemon() already returns
# running=True whenever returncode == 0, regardless of stdout content, so
# a numeric-PID fixture ALREADY passes against the current, unfixed
# implementation -- it isn't RED and adds no discriminating signal. Once
# the coder implements PID parsing per the dash-PID pin above, spot-check
# this positive case as a regression during GREEN verification.
