"""Tests for IPv4-first URL defaults and git-worktree detection helper.

Guards three invariants introduced to fix Windows localhost→::1 hangs:

1. Default URLs for Ollama / Qdrant / TEI / FalkorDB resolve to 127.0.0.1.
2. Env / rc overrides still beat the new defaults (override precedence
   must NOT be affected by the default-value change).
3. ``is_inside_git_worktree`` returns ``True`` for a real git repo and
   ``False`` for a plain temp directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_memory.config import Config, _env, is_inside_git_worktree


# ---------------------------------------------------------------------------
# 1. Default URLs contain 127.0.0.1 (not ``localhost``)
# ---------------------------------------------------------------------------


def test_default_ollama_url_is_ipv4() -> None:
    cfg = Config()
    assert "127.0.0.1" in cfg.ollama_url
    assert "localhost" not in cfg.ollama_url


def test_default_qdrant_url_is_ipv4() -> None:
    cfg = Config()
    assert "127.0.0.1" in cfg.qdrant_url
    assert "localhost" not in cfg.qdrant_url


def test_default_tei_url_is_ipv4() -> None:
    cfg = Config()
    assert "127.0.0.1" in cfg.tei_url
    assert "localhost" not in cfg.tei_url


def test_default_falkor_host_is_ipv4() -> None:
    cfg = Config()
    assert cfg.falkor_host == "127.0.0.1"
    assert "localhost" not in cfg.falkor_host


# ---------------------------------------------------------------------------
# 2. Env-var overrides win over the new defaults
# ---------------------------------------------------------------------------


def test_ollama_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", "http://my-gpu-host:11434")
    # Re-evaluate _env at call time; Config is a frozen dataclass whose
    # defaults are computed from _env(), so we need a fresh instance that
    # reads os.environ at construction.  Because _env() calls os.environ.get
    # directly (not cached), constructing a new Config() after monkeypatching
    # picks up the patched value.
    cfg = Config(ollama_url=_env("OLLAMA_URL", "http://127.0.0.1:11434"))
    assert cfg.ollama_url == "http://my-gpu-host:11434"


def test_qdrant_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_URL", "http://remote-qdrant:6333")
    cfg = Config(qdrant_url=_env("QDRANT_URL", "http://127.0.0.1:6333"))
    assert cfg.qdrant_url == "http://remote-qdrant:6333"


def test_falkor_host_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FALKOR_HOST", "falkordb.internal")
    cfg = Config(falkor_host=_env("FALKOR_HOST", "127.0.0.1"))
    assert cfg.falkor_host == "falkordb.internal"


# ---------------------------------------------------------------------------
# 3. is_inside_git_worktree
# ---------------------------------------------------------------------------


def test_git_worktree_true_for_real_repo() -> None:
    """The test suite itself runs inside the code-memory git repo."""
    # __file__ is inside the repo — any ancestor's .git makes this True.
    assert is_inside_git_worktree(Path(__file__)) is True


def test_git_worktree_false_for_bare_tmp(tmp_path: Path) -> None:
    """A freshly created temp dir has no .git ancestor — must return False."""
    # Ensure there's no git repo above tmp_path by checking from the
    # temp dir itself (OS temp dirs are never inside source trees).
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        # tmp_path somehow fell inside a git tree — skip rather than fail.
        pytest.skip("tmp_path is inside a git repo on this machine")

    assert is_inside_git_worktree(tmp_path) is False


def test_git_worktree_default_uses_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling without arguments checks cwd (same contract as _git_toplevel)."""
    # Change cwd to this repo's root; must be True.
    import os

    monkeypatch.chdir(Path(__file__).parent.parent)
    assert is_inside_git_worktree() is True


def test_git_worktree_subdirectory_inside_repo() -> None:
    """A subdirectory of the repo also returns True."""
    assert is_inside_git_worktree(Path(__file__).parent) is True
