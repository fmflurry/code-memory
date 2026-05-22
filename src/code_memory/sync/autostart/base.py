"""Shared types + platform dispatch for autostart adapters."""

from __future__ import annotations

import logging
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import Adapter

from ...config import detect_project_slug

log = logging.getLogger("codememory.autostart")


@dataclass(frozen=True)
class AutostartStatus:
    installed: bool
    running: bool
    label: str
    unit_path: str | None = None
    note: str | None = None


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
    repo = Path(repo).resolve()
    try:
        adapter = get_adapter()
    except RuntimeError as e:
        return AutostartStatus(
            installed=False,
            running=False,
            label="<unsupported>",
            note=str(e),
        )

    status = adapter.status(repo)
    if status.installed and status.running:
        return status
    if not status.installed:
        status = adapter.install(repo)
    if status.installed and not status.running:
        status = adapter.start(repo)
    return status


def watcher_command(repo: Path) -> list[str]:
    """Resolve the command line that launches the watcher.

    Prefer the installed ``code-memory`` script; fall back to ``python -m``
    invocation when the script isn't on PATH (development checkouts).
    """
    exe = shutil.which("code-memory")
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
