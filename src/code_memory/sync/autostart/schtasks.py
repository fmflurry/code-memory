"""Windows Task Scheduler adapter.

Registers a per-user logon trigger via ``schtasks`` (no admin required).
Uses an XML definition so we can configure restart-on-failure semantics
that the simple ``/Create`` flags don't expose.
"""

from __future__ import annotations

import getpass
import re
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from .base import AutostartStatus, LegacyUnit, repo_label, watcher_command

TASK_FOLDER = "CodeMemory\\Watch"
DAEMON_TASK_NAME = "CodeMemory\\Watchd"


class SchtasksAdapter:
    def _task_name(self, repo: Path) -> str:
        return f"{TASK_FOLDER}\\{repo_label(repo)}"

    def _daemon_task_name(self) -> str:
        return DAEMON_TASK_NAME

    def install(self, repo: Path) -> AutostartStatus:
        repo = Path(repo).resolve()
        argv = watcher_command(repo)
        exe = argv[0]
        args = " ".join(_quote_win(a) for a in argv[1:])
        user = getpass.getuser()
        xml = _TASK_XML_TEMPLATE.format(
            description=escape(f"code-memory watcher for {repo}"),
            user=escape(user),
            exe=escape(exe),
            args=escape(args),
            working_dir=escape(str(repo)),
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".xml", delete=False, encoding="utf-16"
        ) as fh:
            fh.write(xml)
            xml_path = fh.name
        try:
            res = subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/TN",
                    self._task_name(repo),
                    "/XML",
                    xml_path,
                    "/F",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            ok = res.returncode == 0
        finally:
            Path(xml_path).unlink(missing_ok=True)
        return AutostartStatus(
            installed=ok,
            running=False,
            label=self._task_name(repo),
            note=None if ok else res.stderr.strip(),
        )

    def uninstall(self, repo: Path) -> AutostartStatus:
        name = self._task_name(repo)
        subprocess.run(
            ["schtasks", "/Delete", "/TN", name, "/F"],
            capture_output=True,
            check=False,
        )
        return AutostartStatus(installed=False, running=False, label=name)

    def status(self, repo: Path) -> AutostartStatus:
        name = self._task_name(repo)
        out = subprocess.run(
            ["schtasks", "/Query", "/TN", name, "/FO", "LIST"],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            return AutostartStatus(installed=False, running=False, label=name)
        running = "Status:" in out.stdout and "Running" in out.stdout
        return AutostartStatus(installed=True, running=running, label=name)

    def start(self, repo: Path) -> AutostartStatus:
        name = self._task_name(repo)
        subprocess.run(
            ["schtasks", "/Run", "/TN", name],
            capture_output=True,
            check=False,
        )
        return self.status(repo)

    def prune_stale(self) -> list[str]:
        """Remove legacy per-repo tasks whose target dir is gone or ephemeral.

        Self-heals tasks left behind by deleted checkouts and per-session
        worktrees, from before the single fixed daemon task. Returns the
        removed task names. Best-effort.
        """
        from ..safety import is_non_persistent_watch_dir

        out = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/TN", f"{TASK_FOLDER}\\*"],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            return []
        lines = out.stdout.splitlines()
        removed: list[str] = []
        for line in lines[1:]:  # skip CSV header row
            name = line.strip().strip('"')
            if not name:
                continue
            xml_out = subprocess.run(
                ["schtasks", "/Query", "/TN", name, "/XML"],
                capture_output=True,
                text=True,
                check=False,
            )
            if xml_out.returncode != 0:
                continue
            match = re.search(
                r"<WorkingDirectory>(.*?)</WorkingDirectory>", xml_out.stdout
            )
            if not match:
                continue
            target = Path(match.group(1))
            if target.is_dir() and not is_non_persistent_watch_dir(target):
                continue  # live, persistable project — keep
            subprocess.run(
                ["schtasks", "/Delete", "/TN", name, "/F"],
                capture_output=True,
                check=False,
            )
            removed.append(name)
        return removed

    # ------------------------------------------------------------------
    # Single fixed daemon task (registry-driven, no per-repo WorkingDirectory)
    # ------------------------------------------------------------------

    def install_daemon(self) -> AutostartStatus:
        xml = _daemon_task_xml()
        with tempfile.NamedTemporaryFile(
            "w", suffix=".xml", delete=False, encoding="utf-16"
        ) as fh:
            fh.write(xml)
            xml_path = fh.name
        try:
            res = subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/TN",
                    DAEMON_TASK_NAME,
                    "/XML",
                    xml_path,
                    "/F",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            ok = res.returncode == 0
        finally:
            Path(xml_path).unlink(missing_ok=True)
        return AutostartStatus(
            installed=ok,
            running=False,
            label=DAEMON_TASK_NAME,
            note=None if ok else res.stderr.strip(),
        )

    def uninstall_daemon(self) -> AutostartStatus:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", DAEMON_TASK_NAME, "/F"],
            capture_output=True,
            check=False,
        )
        return AutostartStatus(installed=False, running=False, label=DAEMON_TASK_NAME)

    def status_daemon(self) -> AutostartStatus:
        out = subprocess.run(
            ["schtasks", "/Query", "/TN", DAEMON_TASK_NAME, "/FO", "LIST"],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            return AutostartStatus(installed=False, running=False, label=DAEMON_TASK_NAME)
        running = "Status:" in out.stdout and "Running" in out.stdout
        return AutostartStatus(installed=True, running=running, label=DAEMON_TASK_NAME)

    def start_daemon(self) -> AutostartStatus:
        subprocess.run(
            ["schtasks", "/Run", "/TN", DAEMON_TASK_NAME],
            capture_output=True,
            check=False,
        )
        return self.status_daemon()

    # ------------------------------------------------------------------
    # Legacy per-repo unit migration (see ``code-memory autostart migrate``)
    # ------------------------------------------------------------------

    def list_legacy_units(self) -> list[LegacyUnit]:
        """Every legacy per-repo watch task found via schtasks, live or dead."""
        out = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/TN", f"{TASK_FOLDER}\\*"],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            return []
        units: list[LegacyUnit] = []
        for line in out.stdout.splitlines()[1:]:  # skip CSV header row
            name = line.strip().strip('"')
            if not name:
                continue
            xml_out = subprocess.run(
                ["schtasks", "/Query", "/TN", name, "/XML"],
                capture_output=True,
                text=True,
                check=False,
            )
            workdir: str | None = None
            if xml_out.returncode == 0:
                match = re.search(
                    r"<WorkingDirectory>(.*?)</WorkingDirectory>", xml_out.stdout
                )
                if match:
                    workdir = match.group(1)
            units.append({"label": name, "unit_path": name, "workdir": workdir})
        return units

    def remove_legacy_unit(self, unit_path: str) -> None:
        """Idempotent, best-effort deletion of one legacy scheduled task."""
        subprocess.run(
            ["schtasks", "/Delete", "/TN", unit_path, "/F"],
            capture_output=True,
            check=False,
        )


def _quote_win(arg: str) -> str:
    if not arg or any(c in arg for c in ' \t"'):
        return '"' + arg.replace('"', '\\"') + '"'
    return arg


_TASK_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

_DAEMON_TASK_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>{args}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _daemon_task_xml() -> str:
    """XML task definition for the single fixed daemon task.

    Unlike the legacy per-repo template, this has no ``WorkingDirectory``
    element — the daemon is driven by the on-disk watch registry, not a
    single repo root.
    """
    argv = watcher_command()
    exe = argv[0]
    args = " ".join(_quote_win(a) for a in argv[1:])
    user = getpass.getuser()
    return _DAEMON_TASK_XML_TEMPLATE.format(
        description=escape("code-memory watcher daemon"),
        user=escape(user),
        exe=escape(exe),
        args=escape(args),
    )
