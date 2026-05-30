"""Cross-platform autostart registration for the code-memory watcher.

Adapters write a user-level service unit that runs ``code-memory watch <repo>``
at user logon. No root/admin required.

  - macOS   -> launchd LaunchAgent (~/Library/LaunchAgents/*.plist)
  - Linux   -> systemd --user unit (~/.config/systemd/user/*.service)
  - Windows -> Scheduled Task at logon (schtasks /SC ONLOGON /RL LIMITED)
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Protocol

from .base import (
    AutostartStatus,
    ensure_autostart,
    get_adapter,
    prune_stale_autostart,
)
from .launchd import LaunchdAdapter
from .schtasks import SchtasksAdapter
from .systemd import SystemdUserAdapter

__all__ = [
    "AutostartStatus",
    "LaunchdAdapter",
    "SchtasksAdapter",
    "SystemdUserAdapter",
    "ensure_autostart",
    "prune_stale_autostart",
    "get_adapter",
    "Adapter",
]


class Adapter(Protocol):
    def install(self, repo: Path) -> AutostartStatus: ...
    def uninstall(self, repo: Path) -> AutostartStatus: ...
    def status(self, repo: Path) -> AutostartStatus: ...
    def start(self, repo: Path) -> AutostartStatus: ...


# convenience re-export
def current_platform() -> str:
    return platform.system()
