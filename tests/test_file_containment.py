"""``_owning_project`` — deepest-prefix selection for File→Project edges."""

from __future__ import annotations

from pathlib import Path

from code_memory.orchestrator.pipeline import _owning_project


def _proj_dirs(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
    """Sort like the production code does (descending path length)."""
    return sorted(pairs, key=lambda x: -len(x[0]))


def test_picks_deepest_project_when_nested(tmp_path: Path) -> None:
    outer = tmp_path / "A"
    inner = outer / "Sub"
    inner.mkdir(parents=True)
    file_path = inner / "Foo.cs"
    file_path.touch()

    dirs = _proj_dirs(
        (str(outer.resolve()), str(outer / "A.csproj")),
        (str(inner.resolve()), str(inner / "Sub.csproj")),
    )
    assert _owning_project(str(file_path), dirs) == str(inner / "Sub.csproj")


def test_falls_back_to_outer_when_no_inner(tmp_path: Path) -> None:
    outer = tmp_path / "A"
    outer.mkdir()
    file_path = outer / "Foo.cs"
    file_path.touch()

    dirs = _proj_dirs((str(outer.resolve()), str(outer / "A.csproj")))
    assert _owning_project(str(file_path), dirs) == str(outer / "A.csproj")


def test_returns_none_when_file_outside_any_project(tmp_path: Path) -> None:
    a = tmp_path / "A"
    a.mkdir()
    other = tmp_path / "Loose.cs"
    other.touch()

    dirs = _proj_dirs((str(a.resolve()), str(a / "A.csproj")))
    assert _owning_project(str(other), dirs) is None


def test_directory_boundary_respected(tmp_path: Path) -> None:
    """`/repo/A` must not own `/repo/Alpha/x.cs`."""
    a = tmp_path / "A"
    a.mkdir()
    alpha = tmp_path / "Alpha"
    alpha.mkdir()
    file_in_alpha = alpha / "x.cs"
    file_in_alpha.touch()

    dirs = _proj_dirs((str(a.resolve()), str(a / "A.csproj")))
    assert _owning_project(str(file_in_alpha), dirs) is None


def test_empty_project_list_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "x.cs"
    f.touch()
    assert _owning_project(str(f), []) is None
