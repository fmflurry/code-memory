"""RED tests for Phase 4: `code-memory autostart migrate`.

Consolidates legacy per-repo autostart units (launchd/systemd/schtasks,
one unit per watched repo) into the single fixed ``watchd`` daemon
introduced in Phase 3 (see ``test_autostart_daemon.py``), with a STRICT
safety ordering so a root is never left unwatched mid-migration:

    1. seed the on-disk watch registry from every legacy unit
       (``registry.seed_from_units()``)
    2. install + start the single daemon
       (``adapter.install_daemon()`` / ``adapter.start_daemon()``)
    3. VERIFY the running daemon actually covers every seeded root —
       poll ``watchd_state_path()`` (small, injectable timeout/interval)
       until ``pid`` is alive AND ``watched_roots`` is a superset of the
       seeded roots
    4. ONLY once verified: tear down every legacy unit
       (``adapter.remove_legacy_unit(unit_path)`` for each entry
       returned by ``adapter.list_legacy_units()``)
    5. report seeded / verified / removed counts

None of this exists yet. Expected RED failure modes:
  - ``code-memory autostart migrate --help`` -> Click/Typer "No such
    command 'migrate'" usage error (exit code 2).
  - ``registry.seed_from_units()`` slug bug: today it uses the plist
    ``Label`` (e.g. ``com.codememory.watch.gc-webapp``) as the stored
    slug instead of the bare project slug (``gc-webapp``) -> the
    dedicated slug-fix test below fails on a plain equality assertion,
    not a missing-attribute error.

GREEN contract this file pins for the ``coder`` agent
------------------------------------------------------
A) ``code_memory.sync.registry.seed_from_units`` must store the BARE
   slug (``gc-webapp``), not the full launchd label
   (``com.codememory.watch.gc-webapp``), e.g. by stripping
   ``f"{LABEL_PREFIX}."`` from ``Label`` or falling back to
   ``detect_project_slug(workdir)`` / ``Path(workdir).name``.

B) ``code_memory.cli`` gains ``autostart migrate`` on the existing
   ``autostart_app`` sub-app (next to ``install``/``uninstall``/
   ``status`` in ``cli.py``), with signature:

       autostart_app.command("migrate")
       def autostart_migrate(
           dry_run: bool = typer.Option(False, "--dry-run", ...),
           as_json: bool = JsonOpt,   # existing --json flag idiom
       ) -> None: ...

   Behavior, matching the local-import-then-call idiom already used by
   ``watchd`` (patch the *source* module attribute, not a name imported
   into ``cli``'s namespace):
     - ``from .sync import registry`` then ``registry.seed_from_units()``
       -> tests patch ``code_memory.sync.registry.seed_from_units``.
     - ``from .sync.autostart.base import get_adapter`` then
       ``get_adapter()`` -> tests patch
       ``code_memory.sync.autostart.base.get_adapter``.
     - verify polling reads ``watchd_state_path()`` -> tests patch
       ``code_memory.cli.watchd_state_path`` directly (same pattern as
       ``test_cli_watchd.py``).

   Adapter gains two new methods (mocked wholesale in these CLI tests,
   but must be implemented per-platform for real by the coder since
   ``migrate`` calls them at runtime):
     - ``list_legacy_units() -> list[dict]`` — every legacy per-repo
       unit found on disk today (live or dead), each
       ``{"label": str, "unit_path": str, "workdir": str | None}``.
     - ``remove_legacy_unit(unit_path: str) -> None`` — idempotent,
       best-effort boot-out/disable + unlink of ONE legacy unit file.

   Verify-poll knobs, pinned as module attributes on ``code_memory.cli``
   so tests can shrink them (avoid real wall-clock waits):
     - ``MIGRATE_VERIFY_TIMEOUT_S: float`` (default small, e.g. 2.0)
     - ``MIGRATE_VERIFY_INTERVAL_S: float`` (default small, e.g. 0.1)

   Ordering (strict): seed -> list legacy units -> [dry-run: print plan,
   return] -> install_daemon() -> start_daemon() -> poll
   watchd_state_path() until covered or timeout -> if NOT covered: print
   a message containing "incomplete"/"retained" (to be visible in
   ``result.output``) and exit non-zero, WITHOUT calling
   ``remove_legacy_unit`` even once -> if covered: call
   ``remove_legacy_unit(unit["unit_path"])`` for every entry returned by
   ``list_legacy_units()`` (unconditionally — a dead legacy unit that
   was never seeded is still safe to remove since nothing depends on
   it), then exit 0 reporting counts.

   ``--dry-run`` calls ONLY ``registry.seed_from_units()`` (idempotent,
   self-healing — safe to run) and ``adapter.list_legacy_units()``
   (read-only), then prints the plan (seeded roots + which legacy unit
   labels/paths WOULD be removed) and returns. It must NEVER call
   ``install_daemon``, ``start_daemon``, or ``remove_legacy_unit`` — no
   half-migrated state is possible because nothing that mutates daemon
   or legacy-unit state runs at all. Exit 0.

Scope: ``cli.py`` (``autostart migrate`` command) + the slug bug in
``registry.seed_from_units``. Does not touch other phases' test files.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from code_memory.cli import app
from code_memory.sync import registry

runner = CliRunner()

_DARWIN_ONLY = pytest.mark.skipif(
    platform.system() != "Darwin", reason="launchd only on macOS"
)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return xdg


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _write_plist(agents_dir: Path, label: str, workdir: str | None) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "Label": label,
        "ProgramArguments": ["code-memory", "watch", workdir or ""],
    }
    if workdir is not None:
        data["WorkingDirectory"] = workdir
    with (agents_dir / f"{label}.plist").open("wb") as fh:
        plistlib.dump(data, fh)


def _write_state(
    state_path: Path,
    *,
    pid: int,
    roots: list[str],
    ts: float = 1_700_000_000.0,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"pid": pid, "watched_roots": roots, "ts": ts}),
        encoding="utf-8",
    )


def _fast_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the migrate verify-poll knobs so a verify-fail test never
    burns real wall-clock time waiting out the default timeout."""
    from code_memory import cli as cli_mod

    monkeypatch.setattr(cli_mod, "MIGRATE_VERIFY_TIMEOUT_S", 0.05, raising=False)
    monkeypatch.setattr(cli_mod, "MIGRATE_VERIFY_INTERVAL_S", 0.01, raising=False)


def _mock_adapter(
    legacy_units: list[dict[str, str | None]] | None = None,
    order: list[str] | None = None,
) -> tuple[MagicMock, list[str]]:
    order = order if order is not None else []
    adapter = MagicMock()
    adapter.list_legacy_units.return_value = legacy_units or []
    adapter.install_daemon.side_effect = lambda: order.append("install_daemon")
    adapter.start_daemon.side_effect = lambda: order.append("start_daemon")
    adapter.remove_legacy_unit.side_effect = lambda unit_path: order.append(
        f"remove:{unit_path}"
    )
    return adapter, order


# ---------------------------------------------------------------------------
# A. registry.seed_from_units slug-correctness bug fix
# ---------------------------------------------------------------------------


@_DARWIN_ONLY
def test_seed_from_units_slug_is_bare_not_full_launchd_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline bug-fix test.

    Given a legacy plist ``com.codememory.watch.gc-webapp.plist`` with
    ``Label=com.codememory.watch.gc-webapp`` and
    ``WorkingDirectory=<repo>``, the registry entry seeded for that repo
    must carry slug ``gc-webapp`` — the bare project slug — NOT the full
    launchd label ``com.codememory.watch.gc-webapp``. Today's
    implementation does ``slug = str(label) if label else ...``, which
    is exactly the bug this test pins.
    """
    _isolate_registry(tmp_path, monkeypatch)
    fake_home = _fake_home(tmp_path, monkeypatch)
    agents = fake_home / "Library" / "LaunchAgents"
    repo = tmp_path / "gc-webapp"
    repo.mkdir()
    _write_plist(agents, "com.codememory.watch.gc-webapp", str(repo))

    registry.seed_from_units()

    entries = registry.load()
    key = str(repo.resolve())
    assert key in entries, "seeded root must be present in the registry"
    assert entries[key].slug == "gc-webapp", (
        f"expected bare slug 'gc-webapp', got {entries[key].slug!r} — "
        "seed_from_units must strip the 'com.codememory.watch.' label "
        "prefix (or re-derive via detect_project_slug(workdir)/dir "
        "basename), not store the raw plist Label as the slug"
    )


@_DARWIN_ONLY
def test_seed_from_units_slug_matches_repo_dir_name_for_multiword_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second repo/label pair, to make sure the fix isn't hard-coded to
    the single fixture above."""
    _isolate_registry(tmp_path, monkeypatch)
    fake_home = _fake_home(tmp_path, monkeypatch)
    agents = fake_home / "Library" / "LaunchAgents"
    repo = tmp_path / "my-other-repo"
    repo.mkdir()
    _write_plist(agents, "com.codememory.watch.my-other-repo", str(repo))

    registry.seed_from_units()

    entries = registry.load()
    key = str(repo.resolve())
    assert entries[key].slug == "my-other-repo"
    assert not entries[key].slug.startswith("com.codememory.watch")


# ---------------------------------------------------------------------------
# B0. `autostart migrate` must exist at all
# ---------------------------------------------------------------------------


def test_autostart_migrate_command_is_registered() -> None:
    result = runner.invoke(app, ["autostart", "migrate", "--help"])
    assert result.exit_code == 0, (
        f"Expected `autostart migrate --help` to succeed once the command "
        f"is registered, got exit {result.exit_code}.\n{result.output}"
    )


# ---------------------------------------------------------------------------
# B1. happy path: seed -> install+start -> verify -> teardown, exit 0
# ---------------------------------------------------------------------------


def test_migrate_happy_path_seeds_installs_verifies_then_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import cli as cli_mod
    from code_memory.sync import registry as registry_mod
    from code_memory.sync.autostart import base as autostart_base_mod

    _fast_verify(monkeypatch)

    seeded_roots = [str(tmp_path / "repo-a"), str(tmp_path / "repo-b")]
    for r in seeded_roots:
        Path(r).mkdir(parents=True)
    monkeypatch.setattr(registry_mod, "seed_from_units", lambda: list(seeded_roots))

    legacy_units = [
        {
            "label": "com.codememory.watch.repo-a",
            "unit_path": "/agents/a.plist",
            "workdir": seeded_roots[0],
        },
        {
            "label": "com.codememory.watch.repo-b",
            "unit_path": "/agents/b.plist",
            "workdir": seeded_roots[1],
        },
    ]
    adapter, _order = _mock_adapter(legacy_units)
    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: adapter)

    state_path = tmp_path / "watchd-state.json"
    _write_state(
        state_path,
        pid=os.getpid(),
        roots=[str(Path(r).resolve()) for r in seeded_roots],
    )
    monkeypatch.setattr(cli_mod, "watchd_state_path", lambda: state_path)

    result = runner.invoke(app, ["autostart", "migrate"])

    assert result.exit_code == 0, result.output
    adapter.install_daemon.assert_called_once()
    adapter.start_daemon.assert_called_once()
    assert adapter.remove_legacy_unit.call_count == 2, result.output
    removed_paths = {c.args[0] for c in adapter.remove_legacy_unit.call_args_list}
    assert removed_paths == {"/agents/a.plist", "/agents/b.plist"}


# ---------------------------------------------------------------------------
# B2. verify-fail: rollback path — legacy units retained, non-zero exit
# ---------------------------------------------------------------------------


def test_migrate_verify_fail_no_state_file_retains_legacy_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import cli as cli_mod
    from code_memory.sync import registry as registry_mod
    from code_memory.sync.autostart import base as autostart_base_mod

    _fast_verify(monkeypatch)

    seeded_roots = [str(tmp_path / "repo-a")]
    Path(seeded_roots[0]).mkdir(parents=True)
    monkeypatch.setattr(registry_mod, "seed_from_units", lambda: list(seeded_roots))

    legacy_units = [
        {
            "label": "com.codememory.watch.repo-a",
            "unit_path": "/agents/a.plist",
            "workdir": seeded_roots[0],
        }
    ]
    adapter, _order = _mock_adapter(legacy_units)
    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: adapter)

    # No state file at all -> coverage can never be confirmed.
    missing_state_path = tmp_path / "does-not-exist" / "watchd-state.json"
    monkeypatch.setattr(cli_mod, "watchd_state_path", lambda: missing_state_path)

    result = runner.invoke(app, ["autostart", "migrate"])

    assert result.exit_code != 0, (
        f"Expected non-zero exit on verify failure, got 0.\n{result.output}"
    )
    adapter.remove_legacy_unit.assert_not_called()
    lowered = result.output.lower()
    assert "incomplete" in lowered or "retained" in lowered, result.output


def test_migrate_verify_fail_partial_coverage_retains_legacy_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """State file exists and the daemon pid is alive, but ``watched_roots``
    is missing one of the seeded roots — coverage is NOT a superset, so
    this must still take the rollback branch."""
    from code_memory import cli as cli_mod
    from code_memory.sync import registry as registry_mod
    from code_memory.sync.autostart import base as autostart_base_mod

    _fast_verify(monkeypatch)

    seeded_roots = [str(tmp_path / "repo-a"), str(tmp_path / "repo-b")]
    for r in seeded_roots:
        Path(r).mkdir(parents=True)
    monkeypatch.setattr(registry_mod, "seed_from_units", lambda: list(seeded_roots))

    legacy_units = [
        {
            "label": "com.codememory.watch.repo-a",
            "unit_path": "/agents/a.plist",
            "workdir": seeded_roots[0],
        },
        {
            "label": "com.codememory.watch.repo-b",
            "unit_path": "/agents/b.plist",
            "workdir": seeded_roots[1],
        },
    ]
    adapter, _order = _mock_adapter(legacy_units)
    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: adapter)

    state_path = tmp_path / "watchd-state.json"
    # Only repo-a is covered — repo-b is missing from watched_roots.
    _write_state(
        state_path,
        pid=os.getpid(),
        roots=[str(Path(seeded_roots[0]).resolve())],
    )
    monkeypatch.setattr(cli_mod, "watchd_state_path", lambda: state_path)

    result = runner.invoke(app, ["autostart", "migrate"])

    assert result.exit_code != 0, result.output
    adapter.remove_legacy_unit.assert_not_called()
    lowered = result.output.lower()
    assert "incomplete" in lowered or "retained" in lowered, result.output


# ---------------------------------------------------------------------------
# B3. strict ordering — teardown strictly after the verify read
# ---------------------------------------------------------------------------


def test_migrate_teardown_happens_strictly_after_verify_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory import cli as cli_mod
    from code_memory.sync import registry as registry_mod
    from code_memory.sync.autostart import base as autostart_base_mod

    _fast_verify(monkeypatch)

    seeded_roots = [str(tmp_path / "repo-a")]
    Path(seeded_roots[0]).mkdir(parents=True)
    monkeypatch.setattr(registry_mod, "seed_from_units", lambda: list(seeded_roots))

    legacy_units = [
        {
            "label": "com.codememory.watch.repo-a",
            "unit_path": "/agents/a.plist",
            "workdir": seeded_roots[0],
        }
    ]
    order: list[str] = []
    adapter, order = _mock_adapter(legacy_units, order=order)
    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: adapter)

    state_path = tmp_path / "watchd-state.json"
    _write_state(
        state_path,
        pid=os.getpid(),
        roots=[str(Path(seeded_roots[0]).resolve())],
    )

    def fake_watchd_state_path() -> Path:
        order.append("read_state")
        return state_path

    monkeypatch.setattr(cli_mod, "watchd_state_path", fake_watchd_state_path)

    result = runner.invoke(app, ["autostart", "migrate"])

    assert result.exit_code == 0, result.output
    assert "read_state" in order, order
    assert "remove:/agents/a.plist" in order, order
    assert order.index("install_daemon") < order.index("start_daemon"), order
    assert order.index("start_daemon") < order.index("read_state"), order
    assert order.index("read_state") < order.index("remove:/agents/a.plist"), order


# ---------------------------------------------------------------------------
# B4. --dry-run: plan only, no mutation of daemon or legacy units
# ---------------------------------------------------------------------------


def test_migrate_dry_run_prints_plan_without_mutating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.sync import registry as registry_mod
    from code_memory.sync.autostart import base as autostart_base_mod

    seeded_roots = [str(tmp_path / "repo-a")]
    Path(seeded_roots[0]).mkdir(parents=True)
    monkeypatch.setattr(registry_mod, "seed_from_units", lambda: list(seeded_roots))

    legacy_units = [
        {
            "label": "com.codememory.watch.repo-a",
            "unit_path": "/agents/a.plist",
            "workdir": seeded_roots[0],
        }
    ]
    adapter, _order = _mock_adapter(legacy_units)
    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: adapter)

    result = runner.invoke(app, ["autostart", "migrate", "--dry-run"])

    assert result.exit_code == 0, result.output
    adapter.install_daemon.assert_not_called()
    adapter.start_daemon.assert_not_called()
    adapter.remove_legacy_unit.assert_not_called()
    resolved_root = str(Path(seeded_roots[0]).resolve())
    assert resolved_root in result.output or seeded_roots[0] in result.output, (
        result.output
    )
    assert (
        "com.codememory.watch.repo-a" in result.output
        or "/agents/a.plist" in result.output
    ), result.output


def test_migrate_dry_run_json_emits_plan_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.sync import registry as registry_mod
    from code_memory.sync.autostart import base as autostart_base_mod

    seeded_roots = [str(tmp_path / "repo-a")]
    Path(seeded_roots[0]).mkdir(parents=True)
    monkeypatch.setattr(registry_mod, "seed_from_units", lambda: list(seeded_roots))

    legacy_units = [
        {
            "label": "com.codememory.watch.repo-a",
            "unit_path": "/agents/a.plist",
            "workdir": seeded_roots[0],
        }
    ]
    adapter, _order = _mock_adapter(legacy_units)
    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: adapter)

    result = runner.invoke(app, ["autostart", "migrate", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload.get("dry_run") is True
    resolved_root = str(Path(seeded_roots[0]).resolve())
    assert resolved_root in payload.get("seeded_roots", []) or seeded_roots[
        0
    ] in payload.get("seeded_roots", [])
    would_remove = payload.get("would_remove", [])
    assert any(
        "repo-a" in str(item) or "a.plist" in str(item) for item in would_remove
    ), payload
    adapter.install_daemon.assert_not_called()
    adapter.remove_legacy_unit.assert_not_called()


# ---------------------------------------------------------------------------
# B5. idempotent re-run: nothing left to seed/remove is a clean no-op
# ---------------------------------------------------------------------------


def test_migrate_idempotent_rerun_with_nothing_left_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotent rerun: once every legacy unit has already been torn
    down (``list_legacy_units() == []``) and the daemon is already
    healthy, a second ``migrate`` invocation must be a true no-op --
    it must NOT force-restart an already-healthy daemon (no
    ``install_daemon``/``start_daemon`` kickstart), and must still
    report success with nothing removed.
    """
    from code_memory import cli as cli_mod
    from code_memory.sync import registry as registry_mod
    from code_memory.sync.autostart import base as autostart_base_mod

    _fast_verify(monkeypatch)

    monkeypatch.setattr(registry_mod, "seed_from_units", lambda: [])

    adapter, _order = _mock_adapter(legacy_units=[])
    adapter.status_daemon.return_value.running = True
    monkeypatch.setattr(autostart_base_mod, "get_adapter", lambda: adapter)

    state_path = tmp_path / "watchd-state.json"
    _write_state(state_path, pid=os.getpid(), roots=[])
    monkeypatch.setattr(cli_mod, "watchd_state_path", lambda: state_path)

    result = runner.invoke(app, ["autostart", "migrate", "--json"])

    assert result.exit_code == 0, result.output
    adapter.install_daemon.assert_not_called()
    adapter.start_daemon.assert_not_called()
    adapter.remove_legacy_unit.assert_not_called()
    payload = json.loads(result.output)
    assert payload.get("removed") == 0
