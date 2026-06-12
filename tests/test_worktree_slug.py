"""Tests for detect_project_slug() correctness inside a linked git worktree.

Bug: _git_toplevel() uses ``git rev-parse --show-toplevel`` which returns the
WORKTREE's own directory inside a linked worktree, not the main repo root.
This causes a slug mismatch — the linked worktree mints a separate Qdrant /
Falkor namespace and forces a cold full re-ingest.

Desired fix: paths inside a linked worktree must resolve to the MAIN repo's
slug (main worktree toplevel basename), not the worktree dir basename.
Canonical mechanism: ``git rev-parse --path-format=absolute --git-common-dir``
points at the main repo's .git in a linked worktree; its parent is the main
repo root.

Test inventory
--------------
1. ``test_main_repo_slug`` — sanity: main repo root resolves to its own slug.
2. ``test_linked_worktree_slug_matches_main`` — RED: linked worktree root must
   resolve to the MAIN repo slug, not the worktree dir basename.
3. ``test_linked_worktree_subdir_slug_matches_main`` — RED: a subdir INSIDE the
   linked worktree also resolves to the main repo slug.
4. ``test_linked_worktree_is_inside_git_worktree`` — ``is_inside_git_worktree``
   still returns True for a linked worktree root (unchanged contract).
5. ``test_non_git_dir_slug_uses_dirname_fallback`` — regression guard: a
   non-git tmp dir falls back to dirname-based slug and
   ``is_inside_git_worktree`` returns False.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from code_memory.config import detect_project_slug, is_inside_git_worktree, slugify


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_available() -> bool:
    return shutil.which("git") is not None


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, capture_output=True)


def _setup_main_repo(tmp_path: Path) -> Path:
    """Create a minimal real git repo with one commit and return its root."""
    repo = tmp_path / "main-repo"
    repo.mkdir()
    _run(["git", "init", "--initial-branch=main", str(repo)])
    # Fallback for older git that doesn't support --initial-branch
    _run(["git", "-C", str(repo), "config", "user.email", "test@example.com"])
    _run(["git", "-C", str(repo), "config", "user.name", "Test User"])
    (repo / "README.md").write_text("hello", encoding="utf-8")
    _run(["git", "-C", str(repo), "add", "README.md"])
    _run(["git", "-C", str(repo), "commit", "-m", "initial commit"])
    return repo


def _add_worktree(repo: Path, worktree_path: Path, branch: str) -> Path:
    """Add a linked worktree at ``worktree_path`` checked out at ``branch``."""
    _run(
        ["git", "-C", str(repo), "worktree", "add", str(worktree_path), "-b", branch]
    )
    return worktree_path


# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

_SKIP_NO_GIT = pytest.mark.skipif(
    not _git_available(), reason="git binary not available"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def git_worktree_env(tmp_path_factory: pytest.TempPathFactory):
    """
    Set up a main repo + one linked worktree once for the whole module.

    Yields a dict with:
      main_repo     – Path to the main repo root
      worktree_root – Path to the linked worktree root
      main_slug     – expected slugified name of the main repo (baseline)
      worktree_slug – slugified name of the worktree dir (the wrong answer)
    """
    tmp = tmp_path_factory.mktemp("worktree_env")
    main_repo = _setup_main_repo(tmp)
    worktree_root = tmp / "linked-worktree"
    _add_worktree(main_repo, worktree_root, "feature-branch")

    yield {
        "main_repo": main_repo,
        "worktree_root": worktree_root,
        "main_slug": slugify(main_repo.name),
        "worktree_slug": slugify(worktree_root.name),
    }


# ---------------------------------------------------------------------------
# Test 1 — sanity: main repo root → its own slug
# ---------------------------------------------------------------------------


@_SKIP_NO_GIT
def test_main_repo_slug(git_worktree_env: dict) -> None:
    """detect_project_slug(<main repo root>) == slug(main repo dirname)."""
    main_repo: Path = git_worktree_env["main_repo"]
    expected: str = git_worktree_env["main_slug"]

    result = detect_project_slug(main_repo)

    assert result == expected, (
        f"Main repo slug mismatch: got {result!r}, expected {expected!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — RED: linked worktree root must resolve to MAIN repo slug
# ---------------------------------------------------------------------------


@_SKIP_NO_GIT
def test_linked_worktree_slug_matches_main(git_worktree_env: dict) -> None:
    """detect_project_slug(<linked worktree root>) == slug(MAIN repo dirname).

    This is the FAILING (RED) test. Currently _git_toplevel returns the
    worktree's own directory, so the result is slugify('linked-worktree')
    instead of slugify('main-repo').
    """
    worktree_root: Path = git_worktree_env["worktree_root"]
    expected: str = git_worktree_env["main_slug"]
    wrong: str = git_worktree_env["worktree_slug"]

    result = detect_project_slug(worktree_root)

    assert result != wrong, (
        "detect_project_slug returned the worktree dirname slug — the bug is present."
    )
    assert result == expected, (
        f"Linked worktree slug mismatch: got {result!r}, expected {expected!r} "
        f"(worktree dirname slug would be {wrong!r})"
    )


# ---------------------------------------------------------------------------
# Test 3 — RED: subdir inside linked worktree also resolves to MAIN repo slug
# ---------------------------------------------------------------------------


@_SKIP_NO_GIT
def test_linked_worktree_subdir_slug_matches_main(git_worktree_env: dict) -> None:
    """detect_project_slug(<subdir inside linked worktree>) == slug(MAIN repo).

    This is the second FAILING (RED) test. A subdir search walks up to the
    linked worktree root, which currently returns the worktree basename.
    """
    worktree_root: Path = git_worktree_env["worktree_root"]
    expected: str = git_worktree_env["main_slug"]
    wrong: str = git_worktree_env["worktree_slug"]

    # Create a subdir inside the worktree to search from.
    subdir = worktree_root / "src" / "deep"
    subdir.mkdir(parents=True, exist_ok=True)

    result = detect_project_slug(subdir)

    assert result != wrong, (
        "detect_project_slug(subdir) returned the worktree dirname slug — bug present."
    )
    assert result == expected, (
        f"Worktree subdir slug mismatch: got {result!r}, expected {expected!r} "
        f"(worktree dirname slug would be {wrong!r})"
    )


# ---------------------------------------------------------------------------
# Test 4 — is_inside_git_worktree still True for linked worktree root
# ---------------------------------------------------------------------------


@_SKIP_NO_GIT
def test_linked_worktree_is_inside_git_worktree(git_worktree_env: dict) -> None:
    """is_inside_git_worktree(<linked worktree root>) is True (unchanged contract)."""
    worktree_root: Path = git_worktree_env["worktree_root"]

    assert is_inside_git_worktree(worktree_root) is True, (
        "is_inside_git_worktree must still return True for a linked worktree root."
    )


# ---------------------------------------------------------------------------
# Test 5 — regression: non-git dir → dirname fallback, is_inside False
# ---------------------------------------------------------------------------


def test_non_git_dir_slug_uses_dirname_fallback(tmp_path: Path) -> None:
    """A non-git tmp dir falls back to its own basename as the slug.

    Also guards that is_inside_git_worktree returns False for bare directories.
    """
    # Ensure we're working in a directory that is NOT inside any git repo.
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        pytest.skip("tmp_path is unexpectedly inside a git repo on this machine")

    expected_slug = slugify(tmp_path.name)
    assert detect_project_slug(tmp_path) == expected_slug
    assert is_inside_git_worktree(tmp_path) is False
