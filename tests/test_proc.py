"""Tests for code_memory._proc.install_windows_no_window_default().

Mocks Windows behavior without a real Windows host: ``sys.platform`` and
``subprocess.CREATE_NO_WINDOW`` (which does not exist on POSIX) are both
monkeypatched.

CRITICAL flakiness control: ``install_windows_no_window_default()`` mutates
``subprocess.Popen.__init__`` and the module-level ``_INSTALLED`` flag
*directly* (not via ``monkeypatch.setattr``), so pytest's automatic
monkeypatch teardown cannot undo it. An autouse fixture below snapshots and
restores both after every test in this file, and a module-scoped fixture
asserts the real global ``subprocess.Popen.__init__`` is untouched once the
whole file is done, so a failure here can never corrupt other test modules
in the suite.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import code_memory._proc as proc_mod

_REAL_POPEN_INIT = subprocess.Popen.__init__


@pytest.fixture(autouse=True)
def _reset_proc_state():
    """Snapshot + restore ``subprocess.Popen.__init__`` and ``_INSTALLED``.

    Runs around every test in this module so an installed wrapper from one
    test can never leak into the next (or into other test files run in the
    same process).

    It also forces the *uninstalled* state before each test: other test
    modules import ``cli``/``mcp_server``, whose module-level
    ``install_windows_no_window_default()`` may already have run in this
    process (on a real Windows host), leaving ``_INSTALLED=True`` behind.
    """
    orig_popen_init = subprocess.Popen.__init__
    orig_installed = proc_mod._INSTALLED
    subprocess.Popen.__init__ = _REAL_POPEN_INIT
    proc_mod._INSTALLED = False
    yield
    subprocess.Popen.__init__ = orig_popen_init
    proc_mod._INSTALLED = orig_installed


@pytest.fixture(scope="module", autouse=True)
def _assert_global_popen_untouched_after_module():
    yield
    assert subprocess.Popen.__init__ is _REAL_POPEN_INIT, (
        "subprocess.Popen.__init__ leaked out of test_proc.py — a test "
        "failed to restore process-wide global state"
    )


class _FakeSelf:
    """Stand-in for a Popen instance so we can call __init__ directly
    without triggering a real process spawn or Popen's finalizer.
    """


def test_noop_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    orig_init = subprocess.Popen.__init__

    proc_mod.install_windows_no_window_default()

    assert subprocess.Popen.__init__ is orig_init
    assert proc_mod._INSTALLED is False


def test_wraps_popen_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    # subprocess.CREATE_NO_WINDOW only exists on real Windows builds.
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    captured_kwargs: list[dict[str, object]] = []

    def fake_orig_init(self: object, *args: object, **kwargs: object) -> None:
        captured_kwargs.append(kwargs)

    # install_windows_no_window_default() closes over subprocess.Popen.__init__
    # at call time, so the fake must be in place *before* we call it.
    monkeypatch.setattr(subprocess.Popen, "__init__", fake_orig_init)

    proc_mod.install_windows_no_window_default()

    assert proc_mod._INSTALLED is True
    wrapped_init = subprocess.Popen.__init__
    assert wrapped_init is not fake_orig_init, "Popen.__init__ was not wrapped"

    # No explicit creationflags -> CREATE_NO_WINDOW injected.
    wrapped_init(_FakeSelf(), ["some-cmd"])
    assert captured_kwargs[-1].get("creationflags") == 0x08000000

    # Caller already set creationflags -> must not be clobbered.
    wrapped_init(_FakeSelf(), ["some-cmd"], creationflags=0x00000010)
    assert captured_kwargs[-1].get("creationflags") == 0x00000010


def test_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    proc_mod.install_windows_no_window_default()
    assert proc_mod._INSTALLED is True
    wrapped_once = subprocess.Popen.__init__

    proc_mod.install_windows_no_window_default()
    wrapped_twice = subprocess.Popen.__init__

    assert wrapped_twice is wrapped_once, "calling install() twice re-wrapped Popen.__init__"
