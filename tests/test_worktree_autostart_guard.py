"""Tests for the linked-worktree autostart guard.

A persistent OS autostart watcher unit must NOT be registered for a linked
git worktree.  The main repo gets the watcher; a linked worktree is a
secondary checkout sharing the same .git object store, so registering a
per-worktree launchd/systemd/schtasks unit leaks unbounded OS units.

Two new functions will be added by the implementer:

1. ``code_memory.config.is_linked_git_worktree(path)``
   - True when ``path`` is inside a LINKED worktree (not the main worktree).
   - Mechanism: ``git rev-parse --git-dir`` differs from ``--git-common-dir``
     in a linked worktree; they're equal in the main worktree.
   - Pure boolean, never raises; non-git path or missing git returns False;
     main worktree returns False.

2. ``code_memory.sync.safety.is_non_persistent_watch_dir(root)``
   - Returns ``is_ephemeral_watch_dir(root) or is_linked_git_worktree(root)``.
   - The single gate callers use to decide "skip persistent autostart".

Test inventory
--------------
is_linked_git_worktree:
1. ``test_is_linked_git_worktree_true_for_linked_root``
   - is_linked_git_worktree(<linked worktree root>) is True.
2. ``test_is_linked_git_worktree_true_for_subdir_inside_linked``
   - is_linked_git_worktree(<subdir inside linked worktree>) is True.
3. ``test_is_linked_git_worktree_false_for_main_repo``
   - is_linked_git_worktree(<main repo root>) is False.
4. ``test_is_linked_git_worktree_false_for_non_git_dir``
   - is_linked_git_worktree(<non-git tmp dir>) is False.

is_non_persistent_watch_dir:
5. ``test_is_non_persistent_watch_dir_true_for_linked_worktree``
   - True for a linked worktree root.
6. ``test_is_non_persistent_watch_dir_false_for_main_repo``
   - False for the main repo root.
7. ``test_is_non_persistent_watch_dir_true_for_ephemeral_path``
   - True for a path containing .claude/homunculus/<id> segments.

ensure_autostart skip path:
8. ``test_ensure_autostart_skips_linked_worktree``
   - ensure_autostart(<linked worktree root>) returns AutostartStatus with
     .installed is False and a note mentioning worktree/ephemeral skip.
     Short-circuits BEFORE touching any OS adapter.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Skip guard — all tests in this module require the git binary.
# ---------------------------------------------------------------------------

_git_available = shutil.which("git") is not None

_SKIP_NO_GIT = pytest.mark.skipif(
    not _git_available, reason="git binary not available"
)


# ---------------------------------------------------------------------------
# Helpers (mirrors test_worktree_slug.py patterns)
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
    )


def _setup_main_repo(tmp: Path) -> Path:
    """Create a minimal git repo with one commit and return its root."""
    repo = tmp / "main-repo"
    repo.mkdir()
    _run(["git", "init", "--initial-branch=main", str(repo)])
    _run(["git", "-C", str(repo), "config", "user.email", "test@example.com"])
    _run(["git", "-C", str(repo), "config", "user.name", "Test User"])
    (repo / "README.md").write_text("hello", encoding="utf-8")
    _run(["git", "-C", str(repo), "add", "README.md"])
    _run(["git", "-C", str(repo), "commit", "-m", "initial commit"])
    return repo


def _add_worktree(repo: Path, worktree_path: Path, branch: str) -> Path:
    """Add a linked worktree at ``worktree_path`` on a new ``branch``."""
    _run(
        ["git", "-C", str(repo), "worktree", "add", str(worktree_path), "-b", branch]
    )
    return worktree_path


# ---------------------------------------------------------------------------
# Module-scoped fixture: one main repo + one linked worktree for the whole
# module so we don't spin up git processes per test.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def worktree_env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """
    Yields a dict with:
      main_repo     - Path to the main repo root
      worktree_root - Path to the linked worktree root
    """
    tmp = tmp_path_factory.mktemp("worktree_guard_env")
    main_repo = _setup_main_repo(tmp)
    worktree_root = tmp / "linked-worktree"
    _add_worktree(main_repo, worktree_root, "guard-branch")
    return {
        "main_repo": main_repo,
        "worktree_root": worktree_root,
    }


# ---------------------------------------------------------------------------
# Tests 1-4: is_linked_git_worktree
# ---------------------------------------------------------------------------


@_SKIP_NO_GIT
def test_is_linked_git_worktree_true_for_linked_root(worktree_env: dict) -> None:
    """is_linked_git_worktree(<linked worktree root>) is True."""
    from code_memory.config import is_linked_git_worktree  # type: ignore[attr-defined]

    worktree_root: Path = worktree_env["worktree_root"]

    result = is_linked_git_worktree(worktree_root)

    assert result is True, (
        f"Expected True for linked worktree root {worktree_root}, got {result!r}"
    )


@_SKIP_NO_GIT
def test_is_linked_git_worktree_true_for_subdir_inside_linked(
    worktree_env: dict,
) -> None:
    """is_linked_git_worktree(<subdir inside linked worktree>) is True."""
    from code_memory.config import is_linked_git_worktree  # type: ignore[attr-defined]

    worktree_root: Path = worktree_env["worktree_root"]
    subdir = worktree_root / "src" / "deep"
    subdir.mkdir(parents=True, exist_ok=True)

    result = is_linked_git_worktree(subdir)

    assert result is True, (
        f"Expected True for subdir {subdir} inside linked worktree, got {result!r}"
    )


@_SKIP_NO_GIT
def test_is_linked_git_worktree_false_for_main_repo(worktree_env: dict) -> None:
    """is_linked_git_worktree(<main repo root>) is False."""
    from code_memory.config import is_linked_git_worktree  # type: ignore[attr-defined]

    main_repo: Path = worktree_env["main_repo"]

    result = is_linked_git_worktree(main_repo)

    assert result is False, (
        f"Expected False for main repo root {main_repo}, got {result!r}"
    )


def test_is_linked_git_worktree_false_for_non_git_dir(tmp_path: Path) -> None:
    """is_linked_git_worktree(<non-git tmp dir>) is False.

    Does not require the git mark — just needs the tmp dir to be outside
    a git tree.  If tmp_path happens to be inside a git repo on the CI
    runner, we skip rather than fail with an unreliable positive.
    """
    from code_memory.config import is_linked_git_worktree  # type: ignore[attr-defined]

    non_git = tmp_path / "plain-dir"
    non_git.mkdir()

    # Guard: if non_git is inside a git tree on this runner, skip.
    probe = subprocess.run(
        ["git", "-C", str(non_git), "rev-parse", "--git-dir"],
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        pytest.skip("tmp_path is inside a git worktree on this machine — skip")

    result = is_linked_git_worktree(non_git)

    assert result is False, (
        f"Expected False for non-git dir {non_git}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Tests 5-7: is_non_persistent_watch_dir
# ---------------------------------------------------------------------------


@_SKIP_NO_GIT
def test_is_non_persistent_watch_dir_true_for_linked_worktree(
    worktree_env: dict,
) -> None:
    """is_non_persistent_watch_dir returns True for a linked worktree root."""
    from code_memory.sync.safety import is_non_persistent_watch_dir  # type: ignore[attr-defined]

    worktree_root: Path = worktree_env["worktree_root"]

    result = is_non_persistent_watch_dir(worktree_root)

    assert result is True, (
        f"Expected True for linked worktree root {worktree_root}, got {result!r}"
    )


@_SKIP_NO_GIT
def test_is_non_persistent_watch_dir_false_for_main_repo(worktree_env: dict) -> None:
    """is_non_persistent_watch_dir returns False for the main repo root."""
    from code_memory.sync.safety import is_non_persistent_watch_dir  # type: ignore[attr-defined]

    main_repo: Path = worktree_env["main_repo"]

    result = is_non_persistent_watch_dir(main_repo)

    assert result is False, (
        f"Expected False for main repo root {main_repo}, got {result!r}"
    )


def test_is_non_persistent_watch_dir_true_for_ephemeral_path(
    tmp_path: Path,
) -> None:
    """is_non_persistent_watch_dir returns True for a .claude/homunculus/<id> path.

    This reuses existing ephemeral logic via is_ephemeral_watch_dir.
    The path does not need to exist on disk — the check is purely
    path-component based.
    """
    from code_memory.sync.safety import is_non_persistent_watch_dir  # type: ignore[attr-defined]

    ephemeral_path = tmp_path / ".claude" / "homunculus" / "abc123" / "my-proj"
    ephemeral_path.mkdir(parents=True, exist_ok=True)

    result = is_non_persistent_watch_dir(ephemeral_path)

    assert result is True, (
        f"Expected True for ephemeral path {ephemeral_path}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: ensure_autostart short-circuits for linked worktrees
# ---------------------------------------------------------------------------


@_SKIP_NO_GIT
def test_ensure_autostart_skips_linked_worktree(worktree_env: dict) -> None:
    """ensure_autostart(<linked worktree root>) returns installed=False.

    Must short-circuit BEFORE reaching any OS adapter (no launchd/systemd/
    schtasks calls in CI).  The note field must mention the skip reason
    (worktree or ephemeral).
    """
    from code_memory.sync.autostart.base import ensure_autostart

    worktree_root: Path = worktree_env["worktree_root"]

    status = ensure_autostart(worktree_root)

    assert status.installed is False, (
        f"Expected installed=False for linked worktree, got installed={status.installed!r}"
    )
    # The note must give a human-readable reason so operators understand why
    # registration was skipped.  Accept any wording that references worktree
    # or ephemeral / session / persistent concepts.
    note_lower = (status.note or "").lower()
    assert note_lower, "Expected a non-empty note explaining why autostart was skipped"
    assert any(
        keyword in note_lower
        for keyword in ("worktree", "ephemeral", "persistent", "session", "linked")
    ), (
        f"Expected note to mention worktree/ephemeral/persistent/session/linked, "
        f"got: {status.note!r}"
    )
