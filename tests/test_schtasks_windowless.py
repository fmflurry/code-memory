"""Windows-only ``_windowless_watcher_command`` contract, plus a regression
guard pinning that ``_daemon_task_xml()`` must route through it.

These tests run on any OS (they import ``code_memory.sync.autostart.schtasks``
directly and monkeypatch ``sys.executable`` / ``Path.exists`` rather than
requiring a real Windows host or a real ``schtasks`` binary — same style as
``tests/test_autostart_daemon.py``).

Status at time of writing (PR has console-hide changes staged, CRITICAL
daemon-path fix NOT yet applied):

* ``_windowless_watcher_command(repo)`` — the *legacy per-repo* call —
  already exists and should PASS today.
* ``_windowless_watcher_command()`` with **no** ``repo`` — the daemon call —
  targets the INTENDED post-fix signature
  ``_windowless_watcher_command(repo: Path | None = None)``. The current
  signature requires ``repo`` positionally, so calling it with zero
  arguments raises ``TypeError`` today. This is expected RED until the
  coder generalizes the signature.
* ``test_daemon_task_xml_uses_windowless_command`` is the CRITICAL
  regression guard: ``_daemon_task_xml()`` currently calls the plain
  ``watcher_command()`` (see schtasks.py ~line 392), not the windowless
  variant, so a Task Scheduler daemon launch still flashes a console. This
  test MUST be RED against the current tree and MUST turn GREEN once
  ``_daemon_task_xml`` is wired to call ``_windowless_watcher_command()``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_memory.sync.autostart import schtasks as schtasks_mod
from code_memory.sync.autostart.base import watcher_command


def _fake_pythonw_present(monkeypatch: pytest.MonkeyPatch, fake_exe: str) -> Path:
    """Point sys.executable at ``fake_exe`` and make its sibling
    pythonw.exe appear to exist.
    """
    monkeypatch.setattr(schtasks_mod.sys, "executable", fake_exe)
    monkeypatch.setattr(schtasks_mod.Path, "exists", lambda self: True)
    return Path(fake_exe).with_name("pythonw.exe")


def _fake_pythonw_absent(monkeypatch: pytest.MonkeyPatch, fake_exe: str) -> None:
    monkeypatch.setattr(schtasks_mod.sys, "executable", fake_exe)
    monkeypatch.setattr(schtasks_mod.Path, "exists", lambda self: False)


def test_windowless_no_repo_uses_pythonw_watchd(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED against current tree: current signature requires a ``repo`` arg.

    Targets the intended post-fix API:
    ``_windowless_watcher_command(repo: Path | None = None)``.
    """
    pythonw = _fake_pythonw_present(monkeypatch, "/usr/bin/python3.11")

    cmd = schtasks_mod._windowless_watcher_command()

    assert cmd == [str(pythonw), "-m", "code_memory.cli", "watchd"]


def test_windowless_repo_uses_pythonw_watch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Legacy per-repo call — should already pass today."""
    pythonw = _fake_pythonw_present(monkeypatch, "/usr/bin/python3.11")

    cmd = schtasks_mod._windowless_watcher_command(tmp_path)

    assert cmd == [str(pythonw), "-m", "code_memory.cli", "watch", str(tmp_path)]


def test_windowless_falls_back_when_no_pythonw(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No pythonw.exe next to the interpreter -> POSIX-parity fallback."""
    _fake_pythonw_absent(monkeypatch, "/usr/bin/python3.11")

    cmd = schtasks_mod._windowless_watcher_command(tmp_path)

    assert cmd == watcher_command(tmp_path)


def test_daemon_task_xml_uses_windowless_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRITICAL regression guard — MUST be RED against the current tree.

    ``_daemon_task_xml()`` (schtasks.py ~line 385-401) currently builds its
    argv via the plain ``watcher_command()`` from ``base.py``, so a
    Task-Scheduler-launched daemon still allocates a console window. Once
    the fix lands, ``_daemon_task_xml()`` must call
    ``_windowless_watcher_command()`` (generalized to accept no ``repo``)
    instead, and the pythonw path must appear in the generated XML.
    """
    pythonw = _fake_pythonw_present(monkeypatch, "/usr/bin/python3.11")

    xml = schtasks_mod._daemon_task_xml()

    assert str(pythonw) in xml, (
        "daemon task XML does not reference pythonw.exe — "
        "_daemon_task_xml() is still using the plain console watcher_command()"
    )
