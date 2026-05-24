"""Tests for the `.csproj` parser."""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_memory.extractor.csproj import PackageRef, parse_csproj, walk_csprojs


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


SDK_BODY = """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <AssemblyName>Acme.App</AssemblyName>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
    <PackageReference Include="Serilog" Version="3.1.1" />
  </ItemGroup>
  <ItemGroup>
    <ProjectReference Include="..\\Acme.Core\\Acme.Core.csproj" />
  </ItemGroup>
</Project>
"""


CORE_BODY = """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
"""


def _seed(root: Path) -> tuple[Path, Path]:
    app = root / "Acme.App" / "Acme.App.csproj"
    core = root / "Acme.Core" / "Acme.Core.csproj"
    _write(app, SDK_BODY)
    _write(core, CORE_BODY)
    return app, core


def test_parse_basic_sdk_csproj(tmp_path: Path) -> None:
    app, core = _seed(tmp_path)
    info = parse_csproj(app)
    assert info is not None
    assert info.name == "Acme.App"
    assert info.assembly_name == "Acme.App"
    assert info.target_framework == "net8.0"
    assert info.sdk_style is True
    assert info.project_references == [str(core.resolve())]
    assert PackageRef("Newtonsoft.Json", "13.0.3") in info.package_references
    assert PackageRef("Serilog", "3.1.1") in info.package_references


def test_unresolvable_project_reference_dropped(tmp_path: Path) -> None:
    csproj = tmp_path / "Acme.App" / "Acme.App.csproj"
    _write(
        csproj,
        """\
        <Project Sdk="Microsoft.NET.Sdk">
          <ItemGroup>
            <ProjectReference Include="..\\Nowhere\\Nowhere.csproj" />
          </ItemGroup>
        </Project>
        """,
    )
    info = parse_csproj(csproj)
    assert info is not None
    # Reference target doesn't exist on disk → not emitted (no dead nodes).
    assert info.project_references == []


def test_legacy_csproj_with_xmlns(tmp_path: Path) -> None:
    csproj = tmp_path / "Legacy" / "Legacy.csproj"
    _write(
        csproj,
        """\
        <Project ToolsVersion="15.0"
                 xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <TargetFrameworkVersion>v4.8</TargetFrameworkVersion>
            <AssemblyName>Legacy.App</AssemblyName>
          </PropertyGroup>
        </Project>
        """,
    )
    info = parse_csproj(csproj)
    assert info is not None
    assert info.name == "Legacy"
    assert info.assembly_name == "Legacy.App"


def test_target_frameworks_multitarget(tmp_path: Path) -> None:
    csproj = tmp_path / "Multi" / "Multi.csproj"
    _write(
        csproj,
        """\
        <Project Sdk="Microsoft.NET.Sdk">
          <PropertyGroup>
            <TargetFrameworks>net6.0;net8.0</TargetFrameworks>
          </PropertyGroup>
        </Project>
        """,
    )
    info = parse_csproj(csproj)
    assert info is not None
    assert info.target_framework == "net6.0;net8.0"


def test_package_reference_with_version_as_child_element(tmp_path: Path) -> None:
    """Central Package Management style — Version is a child, not attr."""
    csproj = tmp_path / "CPM" / "CPM.csproj"
    _write(
        csproj,
        """\
        <Project Sdk="Microsoft.NET.Sdk">
          <ItemGroup>
            <PackageReference Include="Polly">
              <Version>8.4.1</Version>
            </PackageReference>
          </ItemGroup>
        </Project>
        """,
    )
    info = parse_csproj(csproj)
    assert info is not None
    assert PackageRef("Polly", "8.4.1") in info.package_references


def test_invalid_xml_returns_none(tmp_path: Path) -> None:
    csproj = tmp_path / "Broken" / "Broken.csproj"
    _write(csproj, "<Project Sdk='Microsoft.NET.Sdk'>")  # unclosed
    assert parse_csproj(csproj) is None


def test_walk_csprojs_skips_build_outputs(tmp_path: Path) -> None:
    _seed(tmp_path)
    # Should NOT be picked up — lives under bin/
    _write(
        tmp_path / "Acme.App" / "bin" / "Debug" / "stale.csproj",
        "<Project Sdk='Microsoft.NET.Sdk' />",
    )
    found = walk_csprojs(tmp_path)
    names = sorted(p.name for p in found)
    assert names == ["Acme.App", "Acme.Core"]


def test_walk_csprojs_handles_fsproj(tmp_path: Path) -> None:
    _write(
        tmp_path / "Lib" / "Lib.fsproj",
        """\
        <Project Sdk="Microsoft.NET.Sdk">
          <PropertyGroup>
            <TargetFramework>net8.0</TargetFramework>
          </PropertyGroup>
        </Project>
        """,
    )
    found = walk_csprojs(tmp_path)
    assert [p.name for p in found] == ["Lib"]
