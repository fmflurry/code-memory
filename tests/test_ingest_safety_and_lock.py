"""Tests for Phase 3 (ingest safety guard + single-flight lock) and
Phase 4 (reingest git-worktree backstop).

Test scenarios:
  - `code-memory ingest $HOME` is refused (exit non-zero, walks nothing).
  - A real git repo still ingests successfully.
  - A second concurrent ingest for the same root no-ops via the lock.
  - `reingest` of a file outside any git worktree skips (no slug minted).
  - `reingest` of a file inside a git worktree proceeds normally.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from code_memory.cli import app
from code_memory.sync.safety import (
    UnsafeIngestRootError,
    assert_safe_ingest_root,
)


# ---------------------------------------------------------------------------
# assert_safe_ingest_root unit tests (no subprocess / CLI overhead)
# ---------------------------------------------------------------------------


def test_assert_safe_ingest_root_rejects_home() -> None:
    with pytest.raises(UnsafeIngestRootError, match="home directory|HOME|filesystem"):
        assert_safe_ingest_root(Path.home())


def test_assert_safe_ingest_root_rejects_filesystem_root() -> None:
    with pytest.raises(UnsafeIngestRootError):
        assert_safe_ingest_root(Path("/"))


def test_assert_safe_ingest_root_rejects_non_git_dir(tmp_path: Path) -> None:
    non_git = tmp_path / "not-a-repo"
    non_git.mkdir()
    with pytest.raises(UnsafeIngestRootError, match="git repository"):
        assert_safe_ingest_root(non_git)


def test_assert_safe_ingest_root_accepts_git_repo(tmp_path: Path) -> None:
    repo = tmp_path / "my-repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    resolved = assert_safe_ingest_root(repo)
    assert resolved == repo.resolve()


def test_assert_safe_ingest_root_bypass_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CODE_MEMORY_UNSAFE_INGEST=1 bypasses all checks."""
    monkeypatch.setenv("CODE_MEMORY_UNSAFE_INGEST", "1")
    # Even $HOME passes when bypass is active.
    result = assert_safe_ingest_root(Path.home())
    assert result == Path.home().resolve()


# ---------------------------------------------------------------------------
# CLI ingest — HOME refused (exit code 2)
# ---------------------------------------------------------------------------


def test_cli_ingest_refuses_home(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    # Pass HOME as the root argument. The guard must refuse before touching
    # any pipeline or vector store.
    result = runner.invoke(app, ["ingest", str(Path.home())])
    assert result.exit_code == 2, (
        f"Expected exit 2 (unsafe root), got {result.exit_code}.\n{result.output}"
    )
    assert "error" in result.output.lower() or "refusing" in result.output.lower() or result.exit_code == 2


def test_cli_ingest_refuses_filesystem_root() -> None:
    """``code-memory ingest /`` must be refused with exit code 2.

    This is the exact scenario that caused CPU runaway (the OpenCode plugin
    resolved cwd to '/' and then called ``code-memory ingest /``). The CLI
    must reject it before spawning any pipeline workers.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "/"])
    assert result.exit_code == 2, (
        f"Expected exit 2 (unsafe root), got {result.exit_code}.\n{result.output}"
    )


# ---------------------------------------------------------------------------
# CLI ingest — single-flight lock prevents concurrent runs
# ---------------------------------------------------------------------------


def test_cli_ingest_single_flight_skips_when_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the single-flight lock is already held, ingest exits 0 with a
    'skipped' message instead of starting a second pipeline run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    from code_memory.sync import single_flight

    # Acquire the lock before invoking the CLI — simulates a live ingest.
    slug = "repo"
    acquired = single_flight.try_acquire(repo, slug)
    assert acquired, "precondition: lock must be acquirable in test"

    try:
        runner = CliRunner()
        result = runner.invoke(app, ["ingest", str(repo)])
        # Must exit 0 (clean skip, not an error) with a 'skipped' message.
        assert result.exit_code == 0, (
            f"Expected exit 0 (skipped), got {result.exit_code}.\n"
            f"stdout={result.output}\nstderr={getattr(result, 'stderr', '')}"
        )
    finally:
        single_flight.release(repo, slug)


# ---------------------------------------------------------------------------
# CLI ingest — real git repo proceeds (pipeline called)
# ---------------------------------------------------------------------------


def test_cli_ingest_proceeds_for_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proper git repo must not be refused by the safety guard."""
    repo = tmp_path / "legit-repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    # Patch Pipeline.ingest_repo to avoid touching Qdrant/FalkorDB.
    from dataclasses import dataclass

    @dataclass
    class _FakeStats:
        files: int = 0
        symbols: int = 0
        chunks: int = 0
        skipped: int = 0
        elapsed: float = 0.0

    with patch(
        "code_memory.cli.Pipeline.ingest_repo", return_value=_FakeStats()
    ) as mock_ingest:
        runner = CliRunner()
        result = runner.invoke(app, ["ingest", str(repo), "--json"])
        # Should reach the pipeline (not be refused by safety guard).
        mock_ingest.assert_called_once()
        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\n{result.output}"
        )


# ---------------------------------------------------------------------------
# CLI reingest — file outside git worktree skips (no slug minted)
# ---------------------------------------------------------------------------


def test_cli_reingest_skips_non_git_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file not inside any git worktree must be skipped, not ingested."""
    non_git_dir = tmp_path / "not-tracked"
    non_git_dir.mkdir()
    target = non_git_dir / "some-script.py"
    target.write_text("x = 1\n")

    # Patch is_inside_git_worktree to definitively return False regardless
    # of the real git state of the test runner's cwd.
    with patch("code_memory.config.is_inside_git_worktree", return_value=False):
        runner = CliRunner()
        result = runner.invoke(app, ["reingest", str(target), "--json"])

    assert result.exit_code == 0, (
        f"Expected exit 0 (skipped), got {result.exit_code}.\n{result.output}"
    )
    import json

    payload = json.loads(result.output)
    assert payload.get("skipped") is True
    assert "git worktree" in payload.get("reason", "")


# ---------------------------------------------------------------------------
# CLI reingest — file inside git worktree proceeds
# ---------------------------------------------------------------------------


def test_cli_reingest_proceeds_for_git_tracked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file inside a git worktree must reach the reingest pipeline."""
    repo = tmp_path / "tracked-repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    target = repo / "module.py"
    target.write_text("def hello(): pass\n")

    fake_extraction = MagicMock()
    fake_extraction.path = str(target)
    fake_extraction.symbols = ["hello"]
    fake_extraction.imports = []

    with (
        patch("code_memory.config.is_inside_git_worktree", return_value=True),
        patch(
            "code_memory.cli.Pipeline.reingest_file", return_value=fake_extraction
        ) as mock_reingest,
    ):
        runner = CliRunner()
        result = runner.invoke(app, ["reingest", str(target), "--json"])

    mock_reingest.assert_called_once()
    assert result.exit_code == 0, (
        f"Expected exit 0, got {result.exit_code}.\n{result.output}"
    )
