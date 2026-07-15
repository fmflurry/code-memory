"""RED tests for the watch-registry consolidation (Phase 1).

Two things do not exist yet and must be implemented to make this file pass:

1. ``code_memory.config.watch_registry_path() -> Path``
   - ``XDG_CONFIG_HOME (or ~/.config) / "code-memory" / "watch-registry.json"``
   - Evaluated at *call* time (unlike the module-level ``_GLOBAL_RC``
     constant) so tests can monkeypatch ``XDG_CONFIG_HOME`` per-test.
2. ``code_memory.config.watchd_state_path() -> Path``
   - Sibling of ``watch_registry_path()``, filename ``watchd-state.json``.
3. ``code_memory.sync.registry`` — a new module:
   - ``RegistryEntry(slug: str, added_ts: float)`` — frozen dataclass.
   - ``load() -> dict[str, RegistryEntry]`` keyed by resolved abs path
     string. Missing file / corrupt / truncated JSON -> ``{}``. Never
     raises.
   - ``add(root, slug) -> None`` — resolve path, upsert entry, atomic
     write (temp file + os.replace, no leftover temp file in the target
     dir), idempotent (re-add updates in place, does not duplicate).
   - ``remove(root) -> None`` — drop entry by resolved path; no-op (does
     not raise) if the key is absent; atomic write.
   - ``prune() -> None`` — drop entries whose resolved path no longer
     exists on disk OR for which
     ``code_memory.sync.safety.is_non_persistent_watch_dir(path)`` is
     True. Keeps live, persistent entries.
   - ``seed_from_units() -> list[str]`` — reads legacy launchd plists at
     ``~/Library/LaunchAgents/com.codememory.watch.*.plist``, extracts
     ``WorkingDirectory`` via ``plistlib``, and ``add()``s each *live*
     (still-existing) directory to the registry. Returns the list of
     seeded root paths. Plists with a missing ``WorkingDirectory`` key,
     or whose ``WorkingDirectory`` no longer exists on disk, are skipped
     (not seeded, not counted in the return value).
   - Concurrency: all mutations (``add``/``remove``/``prune``) go through
     an advisory ``fcntl.flock`` file lock + atomic replace so that N
     concurrent writers each touching a distinct key never lose an
     update.

Test inventory
--------------
config helpers:
1. ``test_watch_registry_path_honors_xdg_config_home``
2. ``test_watch_registry_path_falls_back_to_dot_config``
3. ``test_watchd_state_path_is_sibling_of_registry_path``

registry.load:
4. ``test_load_missing_file_returns_empty_dict``
5. ``test_load_valid_file_returns_parsed_entries``
6. ``test_load_corrupt_json_returns_empty_dict``
7. ``test_load_truncated_json_returns_empty_dict``

registry.add:
8. ``test_add_creates_file_and_entry``
9. ``test_add_resolves_symlinked_path``
10. ``test_add_write_is_atomic_no_tmp_residue``
11. ``test_add_idempotent_readd_updates_not_duplicates``
12. ``test_add_does_not_mutate_previously_loaded_snapshot``

registry.remove:
13. ``test_remove_drops_entry``
14. ``test_remove_is_noop_on_absent_key``

registry.prune:
15. ``test_prune_removes_missing_path``
16. ``test_prune_removes_ephemeral_dirs``
17. ``test_prune_keeps_live_persistent_dirs``

concurrency / self-healing:
18. ``test_concurrent_add_preserves_all_entries``
19. ``test_corrupt_json_self_heals_on_next_add``

registry.seed_from_units (launchd fixtures):
20. ``test_seed_from_units_adds_live_launchd_dirs``
21. ``test_seed_from_units_skips_dead_dir``
22. ``test_seed_from_units_skips_missing_working_directory``
23. ``test_seed_from_units_no_agents_dir_returns_empty``

misc:
24. ``test_registry_entry_is_frozen``
"""

from __future__ import annotations

import json
import platform
import plistlib
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from code_memory import config
from code_memory.sync import registry


def _symlinks_supported() -> bool:
    # Windows: needs SeCreateSymbolicLinkPrivilege (Developer Mode or admin).
    with tempfile.TemporaryDirectory() as td:
        try:
            (Path(td) / "probe-link").symlink_to(Path(td))
        except OSError:
            return False
    return True


requires_symlinks = pytest.mark.skipif(
    not _symlinks_supported(),
    reason="symlink creation needs privilege on Windows (Developer Mode or admin)",
)


def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point XDG_CONFIG_HOME at a throwaway dir for the duration of a test.

    Prevents tests from ever touching the real
    ``~/.config/code-memory/watch-registry.json``.
    """
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return xdg


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


# ---------------------------------------------------------------------------
# config helpers
# ---------------------------------------------------------------------------


def test_watch_registry_path_honors_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = config.watch_registry_path()
    assert path == tmp_path / "xdg" / "code-memory" / "watch-registry.json"


def test_watch_registry_path_falls_back_to_dot_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    fake_home = _fake_home(tmp_path, monkeypatch)
    path = config.watch_registry_path()
    assert path == fake_home / ".config" / "code-memory" / "watch-registry.json"


def test_watchd_state_path_is_sibling_of_registry_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    registry_path = config.watch_registry_path()
    state_path = config.watchd_state_path()
    assert state_path.parent == registry_path.parent
    assert state_path.name == "watchd-state.json"


# ---------------------------------------------------------------------------
# registry.load
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    assert registry.load() == {}


def test_load_valid_file_returns_parsed_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    path = config.watch_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    repo_a = str((tmp_path / "repo-a").resolve())
    repo_b = str((tmp_path / "repo-b").resolve())
    path.write_text(
        json.dumps(
            {
                repo_a: {"slug": "repo-a", "added_ts": 111.0},
                repo_b: {"slug": "repo-b", "added_ts": 222.5},
            }
        )
    )

    entries = registry.load()

    assert set(entries) == {repo_a, repo_b}
    assert entries[repo_a].slug == "repo-a"
    assert entries[repo_a].added_ts == 111.0
    assert entries[repo_b].slug == "repo-b"
    assert entries[repo_b].added_ts == 222.5


def test_load_corrupt_json_returns_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    path = config.watch_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json::")

    assert registry.load() == {}


def test_load_truncated_json_returns_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    path = config.watch_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    full = json.dumps({str(tmp_path / "repo"): {"slug": "repo", "added_ts": 1.0}})
    path.write_text(full[: len(full) // 2])  # cut mid-write

    assert registry.load() == {}


# ---------------------------------------------------------------------------
# registry.add
# ---------------------------------------------------------------------------


def test_add_creates_file_and_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    registry.add(repo, "repo-slug")

    path = config.watch_registry_path()
    assert path.is_file()
    entries = registry.load()
    key = str(repo.resolve())
    assert key in entries
    assert entries[key].slug == "repo-slug"
    assert entries[key].added_ts > 0


@requires_symlinks
def test_add_resolves_symlinked_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    link = tmp_path / "link"
    link.symlink_to(repo)

    registry.add(link, "repo-slug")

    entries = registry.load()
    assert str(repo.resolve()) in entries
    assert str(link) not in entries


def test_add_write_is_atomic_no_tmp_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    registry.add(repo, "repo-slug")

    reg_dir = config.watch_registry_path().parent
    # A persistent advisory lock file is a legitimate, intentional part of
    # the locking design (see module docstring) and is not atomic-write
    # residue — only flag leftover *temp-write* files here.
    residual = [p.name for p in reg_dir.iterdir() if "tmp" in p.name.lower()]
    assert residual == []


def test_add_idempotent_readd_updates_not_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    registry.add(repo, "old-slug")
    registry.add(repo, "new-slug")

    entries = registry.load()
    assert len(entries) == 1
    assert entries[str(repo.resolve())].slug == "new-slug"


def test_add_does_not_mutate_previously_loaded_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    repo1 = tmp_path / "repo1"
    repo1.mkdir()
    registry.add(repo1, "one")

    snapshot = registry.load()

    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    registry.add(repo2, "two")

    # The dict object returned by the earlier `load()` call must not have
    # been mutated in place by the later `add()` — immutable-update
    # semantics per project coding standards.
    assert str(repo2.resolve()) not in snapshot
    assert len(snapshot) == 1


# ---------------------------------------------------------------------------
# registry.remove
# ---------------------------------------------------------------------------


def test_remove_drops_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    registry.add(repo, "repo-slug")

    registry.remove(repo)

    assert registry.load() == {}


def test_remove_is_noop_on_absent_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    registry.remove(repo)  # must not raise despite nothing ever added

    assert registry.load() == {}


# ---------------------------------------------------------------------------
# registry.prune
# ---------------------------------------------------------------------------


def test_prune_removes_missing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    _isolate_registry(tmp_path, monkeypatch)
    live = tmp_path / "live"
    live.mkdir()
    ghost = tmp_path / "ghost"
    ghost.mkdir()
    registry.add(live, "live")
    registry.add(ghost, "ghost")
    shutil.rmtree(ghost)

    registry.prune()

    entries = registry.load()
    assert str(live.resolve()) in entries
    assert str(ghost.resolve()) not in entries


def test_prune_removes_ephemeral_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    live = tmp_path / "live"
    live.mkdir()
    eph = tmp_path / "eph"
    eph.mkdir()
    registry.add(live, "live")
    registry.add(eph, "eph")

    def fake_is_non_persistent(path: object) -> bool:
        return Path(path).resolve() == eph.resolve()

    monkeypatch.setattr(registry, "is_non_persistent_watch_dir", fake_is_non_persistent)

    registry.prune()

    entries = registry.load()
    assert str(live.resolve()) in entries
    assert str(eph.resolve()) not in entries


def test_prune_keeps_live_persistent_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    live = tmp_path / "live"
    live.mkdir()
    registry.add(live, "live")
    monkeypatch.setattr(registry, "is_non_persistent_watch_dir", lambda p: False)

    registry.prune()

    assert str(live.resolve()) in registry.load()


# ---------------------------------------------------------------------------
# concurrency / self-healing
# ---------------------------------------------------------------------------


def test_concurrent_add_preserves_all_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    n = 16
    roots = []
    for i in range(n):
        r = tmp_path / f"repo-{i}"
        r.mkdir()
        roots.append(r)

    def worker(root: Path) -> None:
        registry.add(root, root.name)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, roots))

    entries = registry.load()
    assert len(entries) == n
    for r in roots:
        key = str(r.resolve())
        assert key in entries
        assert entries[key].slug == r.name


def test_corrupt_json_self_heals_on_next_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    path = config.watch_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt")
    assert registry.load() == {}

    repo = tmp_path / "repo"
    repo.mkdir()
    registry.add(repo, "repo-slug")

    entries = registry.load()
    assert str(repo.resolve()) in entries
    assert entries[str(repo.resolve())].slug == "repo-slug"


# ---------------------------------------------------------------------------
# registry.seed_from_units (launchd fixtures)
# ---------------------------------------------------------------------------


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


@pytest.mark.skipif(platform.system() != "Darwin", reason="launchd only on macOS")
def test_seed_from_units_adds_live_launchd_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    fake_home = _fake_home(tmp_path, monkeypatch)
    agents = fake_home / "Library" / "LaunchAgents"
    live = tmp_path / "live-repo"
    live.mkdir()
    _write_plist(agents, "com.codememory.watch.live-repo", str(live))

    seeded = registry.seed_from_units()

    assert [str(Path(r).resolve()) for r in seeded] == [str(live.resolve())]
    entries = registry.load()
    assert str(live.resolve()) in entries


@pytest.mark.skipif(platform.system() != "Darwin", reason="launchd only on macOS")
def test_seed_from_units_skips_dead_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    fake_home = _fake_home(tmp_path, monkeypatch)
    agents = fake_home / "Library" / "LaunchAgents"
    dead = tmp_path / "dead-repo"  # deliberately never created
    _write_plist(agents, "com.codememory.watch.dead-repo", str(dead))

    seeded = registry.seed_from_units()

    assert seeded == []
    assert registry.load() == {}


@pytest.mark.skipif(platform.system() != "Darwin", reason="launchd only on macOS")
def test_seed_from_units_skips_missing_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    fake_home = _fake_home(tmp_path, monkeypatch)
    agents = fake_home / "Library" / "LaunchAgents"
    _write_plist(agents, "com.codememory.watch.no-workdir", None)

    seeded = registry.seed_from_units()

    assert seeded == []
    assert registry.load() == {}


@pytest.mark.skipif(platform.system() != "Darwin", reason="launchd only on macOS")
def test_seed_from_units_no_agents_dir_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    _fake_home(tmp_path, monkeypatch)  # LaunchAgents dir intentionally absent

    seeded = registry.seed_from_units()

    assert seeded == []


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def test_registry_entry_is_frozen() -> None:
    entry = registry.RegistryEntry(slug="x", added_ts=1.0)
    with pytest.raises(FrozenInstanceError):
        entry.slug = "y"  # type: ignore[misc]
