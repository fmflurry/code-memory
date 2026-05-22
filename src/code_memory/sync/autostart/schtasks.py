"""Windows Task Scheduler adapter.

Registers a per-user logon trigger via ``schtasks`` (no admin required).
Uses an XML definition so we can configure restart-on-failure semantics
that the simple ``/Create`` flags don't expose.
"""

from __future__ import annotations

import getpass
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from .base import AutostartStatus, repo_label, watcher_command

TASK_FOLDER = "CodeMemory\\Watch"


class SchtasksAdapter:
    def _task_name(self, repo: Path) -> str:
        return f"{TASK_FOLDER}\\{repo_label(repo)}"

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
