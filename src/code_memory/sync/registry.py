"""Consolidated watch registry: one JSON file tracking every root that has
a persistent watcher (launchd/systemd/schtasks unit) registered against it.

Replaces the legacy pattern of discovering watched roots by enumerating
launchd plists (macOS-only, format-coupled) with a single cross-platform
source of truth at :func:`code_memory.config.watch_registry_path`.

On-disk format — JSON object keyed by the *resolved* absolute root path,
values ``{"slug": str, "added_ts": float}``::

    {
      "/Users/x/Workspace/repo-a": {"slug": "repo-a", "added_ts": 1700000000.0}
    }

Concurrency: ``add``/``remove``/``prune`` each take an advisory
``fcntl.flock`` exclusive lock on a sibling lock file for the duration of
their read-modify-write-atomic-replace critical section, so N concurrent
writers touching distinct (or the same) keys never lose an update.
POSIX-only for this phase (``fcntl`` has no Windows equivalent; a
``msvcrt``-based lock is left for a future phase).
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import detect_project_slug, watch_registry_path
from .safety import is_non_persistent_watch_dir


@dataclass(frozen=True)
class RegistryEntry:
    slug: str
    added_ts: float


def _lock_path(registry_path: Path) -> Path:
    return registry_path.with_name(".watch-registry.lock")


def _read_raw(registry_path: Path) -> dict[str, Any]:
    """Read and JSON-parse *registry_path*. Never raises.

    Missing file, corrupt JSON, and truncated JSON all degrade to an
    empty dict — the registry self-heals on the next successful write.
    """
    try:
        text = registry_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _parse_entries(raw: dict[str, Any]) -> dict[str, RegistryEntry]:
    entries: dict[str, RegistryEntry] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        slug = value.get("slug")
        added_ts = value.get("added_ts")
        if not isinstance(slug, str) or not isinstance(added_ts, (int, float)):
            continue
        entries[key] = RegistryEntry(slug=slug, added_ts=float(added_ts))
    return entries


def load() -> dict[str, RegistryEntry]:
    """Return the current registry keyed by resolved absolute root path.

    Missing file / corrupt JSON / truncated JSON all yield ``{}``. Never
    raises.
    """
    registry_path = watch_registry_path()
    return _parse_entries(_read_raw(registry_path))


def _atomic_write(registry_path: Path, raw: dict[str, Any]) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = registry_path.with_name(f".{registry_path.name}.tmp")
    try:
        tmp_path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, registry_path)
    finally:
        # If write_text succeeded but os.replace somehow failed, or if
        # write_text itself raised partway through, don't leave a stray
        # temp file behind.
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _with_lock(registry_path: Path, mutate: Any) -> None:
    """Serialize a read-modify-write-atomic-replace critical section.

    *mutate* receives the current raw dict (parsed, but not yet
    RegistryEntry-typed) and returns the new raw dict to persist.
    """
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(registry_path)
    with lock_path.open("a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            raw = _read_raw(registry_path)
            new_raw = mutate(raw)
            _atomic_write(registry_path, new_raw)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def add(root: Path | str, slug: str) -> None:
    """Upsert an entry for *root* (resolved) with *slug* and the current
    timestamp. Idempotent: re-adding the same root updates the slug and
    timestamp in place rather than duplicating.
    """
    key = str(Path(root).expanduser().resolve())
    registry_path = watch_registry_path()

    def mutate(raw: dict[str, Any]) -> dict[str, Any]:
        updated = dict(raw)
        updated[key] = {"slug": slug, "added_ts": time.time()}
        return updated

    _with_lock(registry_path, mutate)


def remove(root: Path | str) -> None:
    """Drop the entry for *root* (resolved). No-op when the key is absent."""
    key = str(Path(root).expanduser().resolve())
    registry_path = watch_registry_path()

    def mutate(raw: dict[str, Any]) -> dict[str, Any]:
        if key not in raw:
            return raw
        updated = dict(raw)
        del updated[key]
        return updated

    _with_lock(registry_path, mutate)


def prune() -> None:
    """Drop entries whose root no longer exists on disk, or which are now
    non-persistent-watch-eligible (ephemeral dir / linked worktree).
    """
    registry_path = watch_registry_path()

    def mutate(raw: dict[str, Any]) -> dict[str, Any]:
        updated = {}
        for key, value in raw.items():
            path = Path(key)
            if not path.exists():
                continue
            if is_non_persistent_watch_dir(path):
                continue
            updated[key] = value
        return updated

    _with_lock(registry_path, mutate)


def seed_from_units() -> list[str]:
    """Import legacy launchd-registered watch roots into the registry.

    Reads every ``~/Library/LaunchAgents/com.codememory.watch.*.plist``,
    extracts ``WorkingDirectory``, and ``add()``s each directory that
    still exists on disk. Plists that fail to parse, lack a
    ``WorkingDirectory`` key, or whose ``WorkingDirectory`` no longer
    exists are skipped silently.

    Returns the list of root paths that were seeded (as passed to
    ``add()``, before resolution).
    """
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    seeded: list[str] = []
    if not agents_dir.is_dir():
        return seeded

    for plist_path in sorted(agents_dir.glob("com.codememory.watch.*.plist")):
        try:
            with plist_path.open("rb") as fh:
                data = plistlib.load(fh)
        except (OSError, plistlib.InvalidFileException):
            continue
        workdir = data.get("WorkingDirectory")
        if not workdir or not Path(workdir).is_dir():
            continue
        # Re-derive the slug the same way ``repo_label()`` does rather
        # than trusting the plist ``Label`` verbatim — the label carries
        # the full ``com.codememory.watch.<slug>`` prefix, so storing it
        # as-is would corrupt the registry's slug column.
        try:
            slug = detect_project_slug(workdir)
        except Exception:  # noqa: BLE001
            slug = Path(workdir).name
        add(workdir, slug)
        seeded.append(str(workdir))

    return seeded
