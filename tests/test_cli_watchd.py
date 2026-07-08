"""RED tests for Phase 6.1: `code-memory watchd` CLI command.

Pins the GREEN contract for a new Typer command that runs the multi-root
watch daemon (`code_memory.sync.watcher.run_daemon`) in the foreground, plus
a `--status` mode that reads the daemon's on-disk state file instead of
starting anything.

Target design (see GREEN spec returned to orchestrator):
  - `code-memory watchd` calls ``run_daemon(...)`` — imported *locally* inside
    the command function, mirroring the existing ``watch`` command's local
    `from .sync.watcher import run_foreground` import. Tests patch
    ``code_memory.sync.watcher.run_daemon`` so the local import binds to the
    spy at call time.
  - `code-memory watchd --status` does NOT call ``run_daemon`` at all. It
    reads ``watchd_state_path()`` — imported at **module level** in
    ``code_memory.cli`` (alongside the existing `from .config import CONFIG,
    detect_project_slug` line) so tests can patch
    ``code_memory.cli.watchd_state_path`` directly, matching the module's
    existing pattern for `detect_project_slug`.
  - No state file present under `--status`: prints a clear "not running / no
    state" message and exits 0 (pinned below), still without calling
    `run_daemon`.

None of these tests should pass until the `watchd` command exists in
`code_memory/cli.py` — expected RED failure mode is a Typer "No such
command 'watchd'" / usage error (exit code 2), not an assertion bug in this
file.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from code_memory.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# `code-memory watchd` — starts the daemon (run_daemon), clean exit
# ---------------------------------------------------------------------------


def test_watchd_command_is_registered() -> None:
    """`watchd --help` must succeed — i.e. the command must exist at all.

    This is the most basic RED signal: today there is no `watchd` command,
    so Typer/Click reports a usage error (exit code 2) with "No such
    command" in the output.
    """
    result = runner.invoke(app, ["watchd", "--help"])
    assert result.exit_code == 0, (
        f"Expected `watchd --help` to succeed once the command is "
        f"registered, got exit {result.exit_code}.\n{result.output}"
    )


def test_watchd_calls_run_daemon_and_exits_zero_on_clean_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`code-memory watchd` (no flags) must invoke
    `code_memory.sync.watcher.run_daemon` exactly once and exit 0 when
    `run_daemon` returns normally (simulating a clean stop-event trip)."""
    from code_memory.sync import watcher as watcher_mod

    spy = MagicMock(return_value=None)
    monkeypatch.setattr(watcher_mod, "run_daemon", spy)

    result = runner.invoke(app, ["watchd"])

    assert result.exit_code == 0, (
        f"Expected exit 0 on clean run_daemon return, got {result.exit_code}.\n"
        f"{result.output}"
    )
    spy.assert_called_once()


def test_watchd_does_not_call_run_daemon_when_status_flag_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--status` must short-circuit before ever touching `run_daemon` —
    even when a state file exists to read."""
    from code_memory import cli as cli_mod
    from code_memory.sync import watcher as watcher_mod

    state_path = tmp_path / "watchd-state.json"
    state_path.write_text(
        json.dumps({"pid": 4242, "watched_roots": ["/tmp/z"], "ts": 1.0}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_mod, "watchd_state_path", lambda: state_path)

    spy = MagicMock(return_value=None)
    monkeypatch.setattr(watcher_mod, "run_daemon", spy)

    result = runner.invoke(app, ["watchd", "--status"])

    assert result.exit_code == 0, result.output
    spy.assert_not_called()


# ---------------------------------------------------------------------------
# `code-memory watchd --status` — state file present
# ---------------------------------------------------------------------------


def test_watchd_status_prints_pid_and_sorted_roots_from_state_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--status` reads `watchd_state_path()` and prints the pid plus every
    watched root — roots rendered in sorted order regardless of the order
    they were persisted in (mirrors `write_daemon_state`'s own `sorted(...)`
    on write)."""
    from code_memory import cli as cli_mod
    from code_memory.sync import watcher as watcher_mod

    state_path = tmp_path / "watchd-state.json"
    unsorted_roots = ["/tmp/repos/b-repo", "/tmp/repos/a-repo"]
    state_path.write_text(
        json.dumps({"pid": 98765, "watched_roots": unsorted_roots, "ts": 1_700_000_000.0}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_mod, "watchd_state_path", lambda: state_path)
    monkeypatch.setattr(watcher_mod, "run_daemon", MagicMock())

    result = runner.invoke(app, ["watchd", "--status"])

    assert result.exit_code == 0, result.output
    assert "98765" in result.output, result.output
    assert "/tmp/repos/a-repo" in result.output, result.output
    assert "/tmp/repos/b-repo" in result.output, result.output
    # Sorted order: a-repo must render before b-repo.
    assert result.output.index("/tmp/repos/a-repo") < result.output.index(
        "/tmp/repos/b-repo"
    ), result.output


def test_watchd_status_json_flag_emits_full_state_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--status --json` emits the raw state payload as JSON on stdout —
    same `--json` idiom as every other status-style command in this CLI
    (see `sync`, `status`)."""
    from code_memory import cli as cli_mod
    from code_memory.sync import watcher as watcher_mod

    state_path = tmp_path / "watchd-state.json"
    payload = {"pid": 111, "watched_roots": ["/tmp/only-root"], "ts": 42.5}
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(cli_mod, "watchd_state_path", lambda: state_path)
    monkeypatch.setattr(watcher_mod, "run_daemon", MagicMock())

    result = runner.invoke(app, ["watchd", "--status", "--json"])

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed.get("pid") == 111
    assert parsed.get("watched_roots") == ["/tmp/only-root"]


# ---------------------------------------------------------------------------
# `code-memory watchd --status` — no state file (daemon never ran / crashed)
# ---------------------------------------------------------------------------


def test_watchd_status_with_no_state_file_reports_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When `watchd_state_path()` points at a file that doesn't exist, the
    command must print a clear "not running" style message and exit 0
    (pinned: absence of state is a normal, expected condition — not an
    error — matching this CLI's `status` command, which always exits 0
    regardless of what it finds). It must still never call `run_daemon`."""
    from code_memory import cli as cli_mod
    from code_memory.sync import watcher as watcher_mod

    missing_state_path = tmp_path / "does-not-exist" / "watchd-state.json"
    monkeypatch.setattr(cli_mod, "watchd_state_path", lambda: missing_state_path)

    spy = MagicMock(return_value=None)
    monkeypatch.setattr(watcher_mod, "run_daemon", spy)

    result = runner.invoke(app, ["watchd", "--status"])

    assert result.exit_code == 0, (
        f"Expected exit 0 for the no-state-file case, got {result.exit_code}.\n"
        f"{result.output}"
    )
    lowered = result.output.lower()
    assert "not running" in lowered or "no state" in lowered, result.output
    spy.assert_not_called()


def test_watchd_status_json_with_no_state_file_emits_running_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--status --json` with no state file emits a machine-readable
    `{"running": false, ...}`-shaped payload rather than plain text, so
    scripts polling watchd health don't have to string-match prose."""
    from code_memory import cli as cli_mod
    from code_memory.sync import watcher as watcher_mod

    missing_state_path = tmp_path / "nope" / "watchd-state.json"
    monkeypatch.setattr(cli_mod, "watchd_state_path", lambda: missing_state_path)
    monkeypatch.setattr(watcher_mod, "run_daemon", MagicMock())

    result = runner.invoke(app, ["watchd", "--status", "--json"])

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed.get("running") is False
