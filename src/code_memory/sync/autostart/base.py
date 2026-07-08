"""Shared types + platform dispatch for autostart adapters."""

from __future__ import annotations

import logging
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from . import Adapter

from ...config import detect_project_slug
from .. import registry

log = logging.getLogger("codememory.autostart")


@dataclass(frozen=True)
class AutostartStatus:
    installed: bool
    running: bool
    label: str
    unit_path: str | None = None
    note: str | None = None


class LegacyUnit(TypedDict):
    """One legacy per-repo autostart unit found on disk during migration.

    See ``Adapter.list_legacy_units`` / ``code-memory autostart migrate``.
    """

    label: str
    unit_path: str
    workdir: str | None


def get_adapter() -> Adapter:
    """Return the adapter for the current OS."""
    system = platform.system()
    if system == "Darwin":
        from .launchd import LaunchdAdapter

        return LaunchdAdapter()
    if system == "Linux":
        from .systemd import SystemdUserAdapter

        return SystemdUserAdapter()
    if system == "Windows":
        from .schtasks import SchtasksAdapter

        return SchtasksAdapter()
    raise RuntimeError(f"unsupported OS: {system}")


def ensure_autostart(repo: Path, *, project: str | None = None) -> AutostartStatus:
    """Install + start the autostart service for ``repo`` if not already.

    Idempotent. Safe to call on every MCP server boot.
    """
    from ..safety import (
        UnsafeWatchRootError,
        assert_safe_watch_root,
        is_non_persistent_watch_dir,
    )

    try:
        repo = assert_safe_watch_root(repo)
    except UnsafeWatchRootError as e:
        return AutostartStatus(
            installed=False,
            running=False,
            label="<unsafe-root>",
            note=str(e),
        )
    if is_non_persistent_watch_dir(repo):
        return AutostartStatus(
            installed=False,
            running=False,
            label="<ephemeral>",
            note=(
                f"{repo} is an ephemeral / per-session dir or a linked git "
                "worktree; skipping persistent autostart (the main repo's "
                "watcher / session-scoped watcher still applies)."
            ),
        )
    try:
        adapter = get_adapter()
    except RuntimeError as e:
        return AutostartStatus(
            installed=False,
            running=False,
            label="<unsupported>",
            note=str(e),
        )

    slug = project or repo_label(repo)
    registry.add(repo, slug)

    status = adapter.status_daemon()
    if status.installed and status.running:
        return status
    if not status.installed:
        status = adapter.install_daemon()
    if status.installed and not status.running:
        status = adapter.start_daemon()
    return status


def prune_stale_autostart() -> list[str]:
    """Remove autostart units whose target dir is gone or ephemeral, and
    prune the on-disk watch registry to match.

    Best-effort and idempotent. Only adapters that implement ``prune_stale``
    do anything for the legacy per-repo unit sweep; returns the list of
    removed unit labels. ``registry.prune()`` runs unconditionally
    (best-effort, errors logged and swallowed).
    """
    removed: list[str] = []
    try:
        adapter = get_adapter()
    except RuntimeError:
        adapter = None
    if adapter is not None:
        prune = getattr(adapter, "prune_stale", None)
        if prune is not None:
            try:
                removed = list(prune())
            except Exception:  # noqa: BLE001
                log.exception("autostart prune failed")

    try:
        registry.prune()
    except Exception:  # noqa: BLE001
        log.exception("watch registry prune failed")

    return removed


def watcher_command(repo: Path | None = None) -> list[str]:
    """Resolve the command line that launches the watcher.

    Prefer the installed ``code-memory`` script; fall back to ``python -m``
    invocation when the script isn't on PATH (development checkouts).

    With no ``repo`` this returns the single-daemon invocation (``watchd``,
    driven by the on-disk watch registry). With a ``repo`` it returns the
    legacy per-repo invocation (``watch <repo>``), kept for backward
    compatibility with existing per-repo call sites.
    """
    exe = shutil.which("code-memory")
    if repo is None:
        if exe:
            return [exe, "watchd"]
        return [sys.executable, "-m", "code_memory.cli", "watchd"]
    if exe:
        return [exe, "watch", str(repo)]
    return [sys.executable, "-m", "code_memory.cli", "watch", str(repo)]


def repo_label(repo: Path) -> str:
    """Deterministic label / unit name suffix for a repo.

    Uses the same slug logic as project detection so a repo's autostart
    label matches its project slug — easy to spot in `launchctl list`,
    `systemctl --user list-units`, or Task Scheduler.
    """
    try:
        return detect_project_slug(repo)
    except Exception:  # noqa: BLE001
        return repo.name.lower().replace(" ", "-")
