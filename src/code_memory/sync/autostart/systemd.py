"""Linux systemd --user adapter."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .base import AutostartStatus, LegacyUnit, repo_label, watcher_command

UNIT_PREFIX = "codememory-watch"
DAEMON_UNIT = "codememory-watchd.service"


class SystemdUserAdapter:
    def _unit(self, repo: Path) -> str:
        return f"{UNIT_PREFIX}-{repo_label(repo)}.service"

    def _unit_path(self, repo: Path) -> Path:
        return self._units_dir() / self._unit(repo)

    def _units_dir(self) -> Path:
        return (
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "systemd"
            / "user"
        )

    def _daemon_unit_path(self) -> Path:
        return self._units_dir() / DAEMON_UNIT

    # ------------------------------------------------------------------

    def install(self, repo: Path) -> AutostartStatus:
        repo = Path(repo).resolve()
        unit_path = self._unit_path(repo)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        exec_start = " ".join(shlex.quote(arg) for arg in watcher_command(repo))
        unit = f"""[Unit]
Description=code-memory watcher ({repo.name})
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
WorkingDirectory={repo}
Restart=on-failure
RestartSec=5
Environment=PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}

[Install]
WantedBy=default.target
"""
        unit_path.write_text(unit)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            check=False,
        )
        return AutostartStatus(
            installed=True,
            running=False,
            label=self._unit(repo),
            unit_path=str(unit_path),
        )

    def uninstall(self, repo: Path) -> AutostartStatus:
        unit = self._unit(repo)
        path = self._unit_path(repo)
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", unit],
            capture_output=True,
            check=False,
        )
        path.unlink(missing_ok=True)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            check=False,
        )
        return AutostartStatus(
            installed=False, running=False, label=unit, unit_path=str(path)
        )

    def status(self, repo: Path) -> AutostartStatus:
        unit = self._unit(repo)
        path = self._unit_path(repo)
        installed = path.is_file()
        running = False
        if installed:
            out = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True,
                text=True,
                check=False,
            )
            running = out.stdout.strip() == "active"
        return AutostartStatus(
            installed=installed,
            running=running,
            label=unit,
            unit_path=str(path),
        )

    def start(self, repo: Path) -> AutostartStatus:
        unit = self._unit(repo)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", unit],
            capture_output=True,
            check=False,
        )
        # best-effort linger so the service can run without active session
        subprocess.run(
            ["loginctl", "enable-linger", os.getenv("USER", "")],
            capture_output=True,
            check=False,
        )
        return self.status(repo)

    def prune_stale(self) -> list[str]:
        """Remove legacy per-repo units whose target dir is gone or ephemeral.

        Self-heals units left behind by deleted checkouts and per-session
        worktrees, from before the single fixed daemon unit. Returns the
        removed unit filenames. Best-effort.
        """
        from ..safety import is_non_persistent_watch_dir

        units_dir = self._units_dir()
        if not units_dir.is_dir():
            return []
        removed: list[str] = []
        for path in sorted(units_dir.glob(f"{UNIT_PREFIX}-*.service")):
            if path.name == DAEMON_UNIT:
                continue
            try:
                content = path.read_text()
            except OSError:
                continue
            workdir: str | None = None
            for line in content.splitlines():
                if line.startswith("WorkingDirectory="):
                    workdir = line[len("WorkingDirectory=") :].strip()
                    break
            if not workdir:
                continue
            target = Path(workdir)
            if target.is_dir() and not is_non_persistent_watch_dir(target):
                continue  # live, persistable project — keep
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", path.name],
                capture_output=True,
                check=False,
            )
            path.unlink(missing_ok=True)
            removed.append(path.name)
        return removed

    # ------------------------------------------------------------------
    # Single fixed daemon unit (registry-driven, no per-repo WorkingDirectory)
    # ------------------------------------------------------------------

    def install_daemon(self) -> AutostartStatus:
        unit_path = self._daemon_unit_path()
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        exec_start = " ".join(shlex.quote(arg) for arg in watcher_command())
        unit = f"""[Unit]
Description=code-memory watcher daemon
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
Environment=PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}

[Install]
WantedBy=default.target
"""
        unit_path.write_text(unit)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            check=False,
        )
        return AutostartStatus(
            installed=True,
            running=False,
            label=DAEMON_UNIT,
            unit_path=str(unit_path),
        )

    def uninstall_daemon(self) -> AutostartStatus:
        path = self._daemon_unit_path()
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", DAEMON_UNIT],
            capture_output=True,
            check=False,
        )
        path.unlink(missing_ok=True)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            check=False,
        )
        return AutostartStatus(
            installed=False, running=False, label=DAEMON_UNIT, unit_path=str(path)
        )

    def status_daemon(self) -> AutostartStatus:
        path = self._daemon_unit_path()
        installed = path.is_file()
        running = False
        if installed:
            out = subprocess.run(
                ["systemctl", "--user", "is-active", DAEMON_UNIT],
                capture_output=True,
                text=True,
                check=False,
            )
            running = out.stdout.strip() == "active"
        return AutostartStatus(
            installed=installed,
            running=running,
            label=DAEMON_UNIT,
            unit_path=str(path),
        )

    def start_daemon(self) -> AutostartStatus:
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", DAEMON_UNIT],
            capture_output=True,
            check=False,
        )
        # best-effort linger so the service can run without active session
        subprocess.run(
            ["loginctl", "enable-linger", os.getenv("USER", "")],
            capture_output=True,
            check=False,
        )
        return self.status_daemon()

    # ------------------------------------------------------------------
    # Legacy per-repo unit migration (see ``code-memory autostart migrate``)
    # ------------------------------------------------------------------

    def list_legacy_units(self) -> list[LegacyUnit]:
        """Every legacy per-repo watch unit found on disk, live or dead.

        Excludes the single fixed daemon unit (``codememory-watchd.service``).
        """
        units_dir = self._units_dir()
        if not units_dir.is_dir():
            return []
        units: list[LegacyUnit] = []
        for path in sorted(units_dir.glob(f"{UNIT_PREFIX}-*.service")):
            if path.name == DAEMON_UNIT:
                continue
            try:
                content = path.read_text()
            except OSError:
                continue
            workdir: str | None = None
            for line in content.splitlines():
                if line.startswith("WorkingDirectory="):
                    workdir = line[len("WorkingDirectory=") :].strip()
                    break
            units.append(
                {
                    "label": path.name,
                    "unit_path": str(path),
                    "workdir": workdir,
                }
            )
        return units

    def remove_legacy_unit(self, unit_path: str) -> None:
        """Idempotent, best-effort disable + unlink of one legacy unit file."""
        path = Path(unit_path)
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", path.name],
            capture_output=True,
            check=False,
        )
        path.unlink(missing_ok=True)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            check=False,
        )
