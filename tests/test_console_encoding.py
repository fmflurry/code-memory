"""Regression test: _print_plan() must not raise UnicodeEncodeError on cp1252 consoles.

Simulates the Windows PowerShell scenario: stdout is a TextIOWrapper whose
codec is cp1252. The updater prints Unicode decoration characters (→ • · —)
that do not exist in cp1252. Without _force_utf8_console() the call raises
UnicodeEncodeError; after the call it must succeed (chars degrade to '?' at
worst, never a crash).
"""

from __future__ import annotations

import io
import sys

import pytest

from code_memory._console import _force_utf8_console
from code_memory.updater import ComponentState, UpdatePlan, _print_plan


def _cp1252_stdout() -> io.TextIOWrapper:
    """Return a TextIOWrapper backed by a BytesIO, encoded as cp1252."""
    buf = io.BytesIO()
    return io.TextIOWrapper(buf, encoding="cp1252", errors="strict")


def test_print_plan_survives_cp1252_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """_print_plan() must not raise when stdout is a cp1252 stream."""
    fake_stdout = _cp1252_stdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    # _force_utf8_console() reconfigures the stream to utf-8 / errors='replace'
    # before any print() call, just as the real entry points do at startup.
    _force_utf8_console()

    plan = UpdatePlan(
        install_method="uv-tool",
        cli_current="0.7.4",
        cli_latest="0.7.5",
        components=[
            ComponentState(name="CLI", present=True, detail="uv-tool"),
            ComponentState(name="Docker: FalkorDB", present=True, detail="running"),
            ComponentState(name="Docker: Qdrant", present=False),
            ComponentState(name="Ollama: bge-m3", present=True, detail="local"),
            ComponentState(name="Claude Code plugin", present=False),
        ],
    )

    # Must not raise UnicodeEncodeError (or any other exception).
    _print_plan(plan)

    fake_stdout.flush()


def test_force_utf8_console_is_noop_on_stringio(monkeypatch: pytest.MonkeyPatch) -> None:
    """_force_utf8_console() must not crash when stdout has no reconfigure()."""
    fake_stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    # StringIO has no reconfigure — the helper must silently skip it.
    _force_utf8_console()
    # Writing Unicode must still work after the no-op call.
    print("→ • ✓ —", file=sys.stdout)
    assert "→" in fake_stdout.getvalue()
