"""macOS launchd LaunchAgent adapter."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from .base import AutostartStatus, LegacyUnit, repo_label, watcher_command

LABEL_PREFIX = "com.codememory.watch"
DAEMON_LABEL = "com.codememory.watchd"


class LaunchdAdapter:
    def _label(self, repo: Path) -> str:
        return f"{LABEL_PREFIX}.{repo_label(repo)}"

    def _plist_path(self, repo: Path) -> Path:
        agents = Path.home() / "Library" / "LaunchAgents"
        return agents / f"{self._label(repo)}.plist"

    def _daemon_plist_path(self) -> Path:
        agents = Path.home() / "Library" / "LaunchAgents"
        return agents / f"{DAEMON_LABEL}.plist"

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

    def prune_stale(self) -> list[str]:
        """Boot out + remove watch agents whose target dir is gone or ephemeral.

        Self-heals units left behind by deleted checkouts and per-session
        worktrees. Returns the removed labels. Best-effort.
        """
        from ..safety import is_non_persistent_watch_dir

        agents = Path.home() / "Library" / "LaunchAgents"
        if not agents.is_dir():
            return []
        domain = f"gui/{os.getuid()}"
        removed: list[str] = []
        for path in sorted(agents.glob(f"{LABEL_PREFIX}.*.plist")):
            try:
                with path.open("rb") as fh:
                    data = plistlib.load(fh)
            except (OSError, plistlib.InvalidFileException):
                continue
            workdir = data.get("WorkingDirectory")
            if not workdir:
                continue
            target = Path(workdir)
            if target.is_dir() and not is_non_persistent_watch_dir(target):
                continue  # live, persistable project — keep
            label = data.get("Label", path.stem)
            subprocess.run(
                ["launchctl", "bootout", domain, str(path)],
                capture_output=True,
                check=False,
            )
            path.unlink(missing_ok=True)
            removed.append(label)
        return removed

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

    # ------------------------------------------------------------------
    # Single fixed daemon unit (registry-driven, no per-repo WorkingDirectory)
    # ------------------------------------------------------------------

    def install_daemon(self) -> AutostartStatus:
        path = self._daemon_plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        logs = self._logs_dir()
        logs.mkdir(parents=True, exist_ok=True)

        plist = {
            "Label": DAEMON_LABEL,
            "ProgramArguments": watcher_command(),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / f"{DAEMON_LABEL}.log"),
            "StandardErrorPath": str(logs / f"{DAEMON_LABEL}.err"),
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
            label=DAEMON_LABEL,
            unit_path=str(path),
        )

    def uninstall_daemon(self) -> AutostartStatus:
        path = self._daemon_plist_path()
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(path)],
            capture_output=True,
            check=False,
        )
        path.unlink(missing_ok=True)
        return AutostartStatus(
            installed=False, running=False, label=DAEMON_LABEL, unit_path=str(path)
        )

    def status_daemon(self) -> AutostartStatus:
        path = self._daemon_plist_path()
        installed = path.is_file()
        running = False
        if installed:
            out = subprocess.run(
                ["launchctl", "list"],
                capture_output=True,
                text=True,
                check=False,
            )
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    parts = line.split("\t")
                    if not parts:
                        continue
                    if parts[-1].strip() == DAEMON_LABEL:
                        running = parts[0].strip().isdigit()
                        break
        return AutostartStatus(
            installed=installed,
            running=running,
            label=DAEMON_LABEL,
            unit_path=str(path),
        )

    def start_daemon(self) -> AutostartStatus:
        path = self._daemon_plist_path()
        domain = f"gui/{os.getuid()}"
        # bootstrap (may fail if already loaded — that's fine)
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(path)],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["launchctl", "enable", f"{domain}/{DAEMON_LABEL}"],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"{domain}/{DAEMON_LABEL}"],
            capture_output=True,
            check=False,
        )
        return self.status_daemon()

    # ------------------------------------------------------------------
    # Legacy per-repo unit migration (see ``code-memory autostart migrate``)
    # ------------------------------------------------------------------

    def list_legacy_units(self) -> list[LegacyUnit]:
        """Every legacy per-repo watch plist found on disk, live or dead.

        Excludes the single fixed daemon unit
        (``com.codememory.watchd.plist``) — the glob pattern already
        can't match it since there's no literal ``.`` between ``watch``
        and ``d`` in that filename.
        """
        agents = Path.home() / "Library" / "LaunchAgents"
        if not agents.is_dir():
            return []
        units: list[LegacyUnit] = []
        for path in sorted(agents.glob(f"{LABEL_PREFIX}.*.plist")):
            try:
                with path.open("rb") as fh:
                    data = plistlib.load(fh)
            except (OSError, plistlib.InvalidFileException):
                continue
            label = data.get("Label", path.stem)
            workdir = data.get("WorkingDirectory")
            units.append(
                {
                    "label": str(label),
                    "unit_path": str(path),
                    "workdir": str(workdir) if workdir else None,
                }
            )
        return units

    def remove_legacy_unit(self, unit_path: str) -> None:
        """Idempotent, best-effort boot-out + unlink of one legacy plist."""
        path = Path(unit_path)
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(path)],
            capture_output=True,
            check=False,
        )
        path.unlink(missing_ok=True)
