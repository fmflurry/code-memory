"""Linux systemd --user adapter."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .base import AutostartStatus, repo_label, watcher_command

UNIT_PREFIX = "codememory-watch"


class SystemdUserAdapter:
    def _unit(self, repo: Path) -> str:
        return f"{UNIT_PREFIX}-{repo_label(repo)}.service"

    def _unit_path(self, repo: Path) -> Path:
        base = (
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "systemd"
            / "user"
        )
        return base / self._unit(repo)

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
