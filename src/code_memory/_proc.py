"""Process-spawn helpers.

code-memory shells out constantly — git plumbing, ``schtasks``, and
self-invocations of its own CLI — and it does so from long-lived processes
that have **no console of their own**: the MCP server (launched hidden by the
coding agent) and the ``pythonw.exe`` watcher. On Windows, when a
console-subsystem child is spawned by a parent that has no console, the OS
allocates a brand-new console window for it — a visible ``cmd`` flash for every
single call. Multiply that by a file-watcher firing on every save and it reads
as "cmd windows popping up constantly".

``CREATE_NO_WINDOW`` tells Windows not to allocate that console. Rather than
thread the flag through every scattered ``subprocess`` call site, we install it
once as the process-wide default at each entry point (see the callers of
:func:`install_windows_no_window_default`). No-op on POSIX.
"""

from __future__ import annotations

import subprocess
import sys

_INSTALLED = False


def install_windows_no_window_default() -> None:
    """Make every ``subprocess`` spawn in this process windowless on Windows.

    Wraps :class:`subprocess.Popen` so each child inherits
    ``CREATE_NO_WINDOW`` unless the caller explicitly asked for other
    creation flags. Idempotent and a no-op off Windows. Safe to call from
    multiple entry points.
    """
    global _INSTALLED
    if _INSTALLED or sys.platform != "win32":
        return

    no_window = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    orig_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Only inject when the caller hasn't set its own creation flags, so we
        # never clobber an intentional CREATE_NEW_CONSOLE / DETACHED_PROCESS.
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = no_window
        orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init  # type: ignore[assignment,method-assign]
    _INSTALLED = True
