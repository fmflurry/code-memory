"""Tests for project-slug detection sentinel handling.

Open-weight models often pass ``project="auto"`` or omit the param when
they don't know the slug. The server must treat sentinel env values as
"infer from cwd" instead of as literal namespaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_memory.config import detect_project_slug, slugify


@pytest.mark.parametrize("sentinel", ["auto", "AUTO", "Auto", "default", "", "  "])
def test_env_sentinel_falls_through_to_cwd(
    sentinel: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CODE_MEMORY_PROJECT", sentinel)
    monkeypatch.chdir(tmp_path)
    slug = detect_project_slug()
    # cwd is tmp_path, which has no .git → slug derived from tmp_path name
    assert slug == slugify(tmp_path.name)
    assert slug not in {"auto", "default", ""}


def test_env_real_slug_is_respected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CODE_MEMORY_PROJECT", "my-actual-project")
    monkeypatch.chdir(tmp_path)
    assert detect_project_slug() == "my-actual-project"


def test_env_real_slug_gets_slugified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CODE_MEMORY_PROJECT", "My Project (v2)")
    monkeypatch.chdir(tmp_path)
    assert detect_project_slug() == "my-project-v2"


def test_explicit_root_overrides_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CODE_MEMORY_PROJECT", "from-env")
    other = tmp_path / "different-repo"
    other.mkdir()
    assert detect_project_slug(other) == "different-repo"
