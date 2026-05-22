"""macOS launchd LaunchAgent adapter."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from .base import AutostartStatus, repo_label, watcher_command

LABEL_PREFIX = "com.codememory.watch"


class LaunchdAdapter:
    def _label(self, repo: Path) -> str:
        return f"{LABEL_PREFIX}.{repo_label(repo)}"

    def _plist_path(self, repo: Path) -> Path:
        agents = Path.home() / "Library" / "LaunchAgents"
        return agents / f"{self._label(repo)}.plist"

    def _logs_dir(self) -> Path:
        return Path.home() / "Library" / "Logs" / "codememory"

    # ------------------------------------------------------------------

    def install(self, repo: Path) -> AutostartStatus:
        repo = Path(repo).resolve()
        path = self._plist_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        logs = self._logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        label = self._label(repo)

        plist = {
            "Label": label,
            "ProgramArguments": watcher_command(repo),
            "WorkingDirectory": str(repo),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / f"{label}.log"),
            "StandardErrorPath": str(logs / f"{label}.err"),
            "ProcessType": "Background",
            "EnvironmentVariables": {
                "PATH": os.environ.get(
                    "PATH",
                    "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
                ),
            },
        }
        with path.open("wb") as fh:
            plistlib.dump(plist, fh)
        return AutostartStatus(
            installed=True,
            running=False,
            label=label,
            unit_path=str(path),
        )

    def uninstall(self, repo: Path) -> AutostartStatus:
        label = self._label(repo)
        path = self._plist_path(repo)
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(path)],
            capture_output=True,
            check=False,
        )
        path.unlink(missing_ok=True)
        return AutostartStatus(
            installed=False, running=False, label=label, unit_path=str(path)
        )

    def status(self, repo: Path) -> AutostartStatus:
        label = self._label(repo)
        path = self._plist_path(repo)
        installed = path.is_file()
        running = False
        if installed:
            out = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True,
                text=True,
                check=False,
            )
            running = out.returncode == 0
        return AutostartStatus(
            installed=installed,
            running=running,
            label=label,
            unit_path=str(path),
        )

    def start(self, repo: Path) -> AutostartStatus:
        label = self._label(repo)
        path = self._plist_path(repo)
        domain = f"gui/{os.getuid()}"
        # bootstrap (may fail if already loaded — that's fine)
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(path)],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["launchctl", "enable", f"{domain}/{label}"],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
            capture_output=True,
            check=False,
        )
        return self.status(repo)
