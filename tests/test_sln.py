"""Tests for the `.sln` parser."""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_memory.extractor.sln import parse_sln, walk_solutions


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


SLN_BODY = """\
Microsoft Visual Studio Solution File, Format Version 12.00
# Visual Studio Version 17
Project("{9A19103F-16F7-4668-BE54-9A1E7A4F7556}") = "Acme.App", "Acme.App\\Acme.App.csproj", "{5E2F2E60-19A3-4961-8505-9E5F22B1373F}"
EndProject
Project("{9A19103F-16F7-4668-BE54-9A1E7A4F7556}") = "Acme.Core", "Acme.Core\\Acme.Core.csproj", "{F343D293-ADA5-4A21-9DD0-9C4AA6D45C8A}"
EndProject
Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = "Solution Items", "Solution Items", "{D9E12345-1234-1234-1234-1234567890AB}"
EndProject
Global
EndGlobal
"""


def _seed(root: Path) -> Path:
    sln = root / "Acme.sln"
    _write(sln, SLN_BODY)
    # The .sln references projects that need to exist on disk.
    _write(root / "Acme.App" / "Acme.App.csproj", "<Project />")
    _write(root / "Acme.Core" / "Acme.Core.csproj", "<Project />")
    return sln


def test_parses_project_entries(tmp_path: Path) -> None:
    sln = _seed(tmp_path)
    info = parse_sln(sln)
    assert info is not None
    assert info.name == "Acme"
    # Two real projects; the "Solution Items" folder is dropped.
    assert {p.name for p in info.projects} == {"Acme.App", "Acme.Core"}
    paths = {p.csproj_path for p in info.projects}
    assert str((tmp_path / "Acme.App" / "Acme.App.csproj").resolve()) in paths


def test_drops_missing_project_references(tmp_path: Path) -> None:
    """Projects whose path is outside the repo (or missing) are skipped."""
    sln = _seed(tmp_path)
    # Delete one project — its entry in the .sln becomes a dangling ref.
    (tmp_path / "Acme.Core" / "Acme.Core.csproj").unlink()
    info = parse_sln(sln)
    assert info is not None
    assert [p.name for p in info.projects] == ["Acme.App"]


def test_drops_solution_folder_entries(tmp_path: Path) -> None:
    """Type GUID 2150E333 == solution folder, not a buildable project."""
    sln = _seed(tmp_path)
    info = parse_sln(sln)
    assert info is not None
    assert all(p.type_guid != "2150e333-8fdc-42a3-9474-1a3956d46de8" for p in info.projects)


def test_handles_utf8_bom(tmp_path: Path) -> None:
    sln = tmp_path / "Bom.sln"
    sln.parent.mkdir(parents=True, exist_ok=True)
    sln.write_bytes(b"\xef\xbb\xbf" + SLN_BODY.encode("utf-8"))
    (tmp_path / "Acme.App" / "Acme.App.csproj").parent.mkdir(parents=True)
    (tmp_path / "Acme.App" / "Acme.App.csproj").write_text("<Project />")
    (tmp_path / "Acme.Core" / "Acme.Core.csproj").parent.mkdir(parents=True)
    (tmp_path / "Acme.Core" / "Acme.Core.csproj").write_text("<Project />")
    info = parse_sln(sln)
    assert info is not None
    assert len(info.projects) == 2


def test_walk_solutions_skips_build_outputs(tmp_path: Path) -> None:
    _seed(tmp_path)
    # Should NOT be picked up — lives under bin/
    stale = tmp_path / "bin" / "Acme.sln"
    _write(stale, SLN_BODY)
    found = walk_solutions(tmp_path)
    names = sorted(s.name for s in found)
    assert names == ["Acme"]


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert parse_sln(tmp_path / "missing.sln") is None


def test_handles_windows_path_separators(tmp_path: Path) -> None:
    """The .sln above uses backslashes; parser must normalize them."""
    sln = _seed(tmp_path)
    info = parse_sln(sln)
    assert info is not None
    # No path should contain a literal backslash on POSIX.
    assert all("\\" not in p.csproj_path for p in info.projects)
