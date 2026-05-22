"""Default exclude list filters noisy build/cache/agent dirs at repo root."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_memory.sync.watcher import _default_exclude

# Dirs that must be excluded at repo root.
NOISY_ROOT_DIRS = [
    ".git",
    ".venv",
    "node_modules",
    "data",
    # Build outputs
    "dist",
    "out-tsc",
    "build",
    "target",
    "coverage",
    # Framework caches
    ".angular",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".parcel-cache",
    ".cache",
    # Python caches
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    # Editors
    ".idea",
    ".vscode",
    # Agentic tool caches
    ".opencode",
    ".serena",
    ".claude",
    ".cursor",
    ".windsurf",
    ".clavix",
]


@pytest.mark.parametrize("name", NOISY_ROOT_DIRS)
def test_noisy_root_dir_is_excluded(tmp_path: Path, name: str) -> None:
    d = tmp_path / name
    d.mkdir()
    f = d / "x"
    f.write_text("")

    exclude = _default_exclude(tmp_path)

    assert exclude(d), f"dir {name} should be excluded"
    assert exclude(f), f"file inside {name} should be excluded"


def test_source_files_not_excluded(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    f = src / "main.py"
    f.write_text("")

    exclude = _default_exclude(tmp_path)

    assert not exclude(src)
    assert not exclude(f)


def test_similarly_named_dir_inside_source_not_excluded(tmp_path: Path) -> None:
    """A dir named like a noisy root dir but nested under source is still walked.

    Example: ``src/dist/foo.ts`` is project code that happens to be in a folder
    called ``dist`` — not a build output. We anchor exclude to repo root.
    """
    nested = tmp_path / "src" / "dist"
    nested.mkdir(parents=True)
    f = nested / "foo.ts"
    f.write_text("")

    exclude = _default_exclude(tmp_path)

    assert not exclude(nested)
    assert not exclude(f)


def test_missing_dir_does_not_raise(tmp_path: Path) -> None:
    exclude = _default_exclude(tmp_path)
    assert not exclude(tmp_path / "does_not_exist.py")
