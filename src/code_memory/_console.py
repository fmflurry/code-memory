"""Console encoding utilities.

Ensures stdout/stderr are safe on non-UTF-8 consoles (e.g. Windows cp1252).
Must be imported with zero heavy dependencies so it can be called at the
earliest point of every CLI entry point.
"""

from __future__ import annotations

import sys


def _force_utf8_console() -> None:
    """Reconfigure stdout and stderr to UTF-8 with errors='replace'.

    On Windows the console inherits the active code page (often cp1252 / cp850).
    Python's ``TextIOWrapper.reconfigure`` switches to UTF-8 while keeping the
    same underlying buffer, so Unicode decoration chars (→ • ✓ —) render
    correctly on a real Windows console (Python uses the Unicode console API)
    and degrade to ``?`` when redirected to a cp1252 pipe — never a crash.

    The ``getattr`` guard + bare ``except Exception`` make this a safe no-op
    on stream types that lack ``reconfigure`` (e.g. pytest's ``CaptureFixture``
    or ``io.StringIO``).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
