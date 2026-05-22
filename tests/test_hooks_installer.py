"""Git hooks installer: idempotency + marker-block handling."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from code_memory.sync.hooks import (
    HOOKS,
    MARKER_END,
    MARKER_START,
    hook_status,
    install_hooks,
    uninstall_hooks,
)


def _has_git() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not on PATH")


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True
    )


def test_install_creates_all_hooks(tmp_path: Path) -> None:
    _init(tmp_path)
    result = install_hooks(tmp_path)
    assert set(result.installed) == set(HOOKS)
    for hook in HOOKS:
        path = Path(result.hooks_dir) / hook
        assert path.is_file()
        content = path.read_text()
        assert MARKER_START in content
        assert MARKER_END in content


def test_install_is_idempotent(tmp_path: Path) -> None:
    _init(tmp_path)
    install_hooks(tmp_path)
    second = install_hooks(tmp_path)
    # second run: identical block already present -> skipped
    assert set(second.skipped) == set(HOOKS)
    assert second.installed == []


def test_install_preserves_user_hook_content(tmp_path: Path) -> None:
    _init(tmp_path)
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    user_path = hooks_dir / "post-commit"
    user_path.write_text("#!/usr/bin/env bash\necho 'user logic'\n")
    install_hooks(tmp_path)
    content = user_path.read_text()
    assert "echo 'user logic'" in content
    assert MARKER_START in content


def test_status_reflects_install_state(tmp_path: Path) -> None:
    _init(tmp_path)
    pre = hook_status(tmp_path)
    assert all(not v for v in pre.values())
    install_hooks(tmp_path)
    post = hook_status(tmp_path)
    assert all(post[h] for h in HOOKS)


def test_uninstall_removes_block_only(tmp_path: Path) -> None:
    _init(tmp_path)
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "post-commit").write_text(
        "#!/usr/bin/env bash\necho 'user logic'\n"
    )
    install_hooks(tmp_path)
    uninstall_hooks(tmp_path)
    content = (hooks_dir / "post-commit").read_text()
    assert "user logic" in content
    assert MARKER_START not in content
