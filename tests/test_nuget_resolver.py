"""Tests for NuGet / project-output DLL resolution.

All tests build a fake on-disk NuGet cache + bin/ layout under
``tmp_path`` so we don't depend on the user's real ``~/.nuget``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_memory.extractor.csproj import CsprojInfo, PackageRef
from code_memory.extractor.nuget import (
    _candidate_tfms,
    _version_sort_key,
    resolve_package_dlls,
    resolve_project_reference_dlls,
    resolve_refs,
)


# --------------------------------------------------------------- _candidate_tfms


def test_unknown_tfm_returns_itself_only() -> None:
    assert _candidate_tfms("netcoreapp9.42") == ["netcoreapp9.42"]


def test_net8_falls_back_through_netstandard() -> None:
    chain = _candidate_tfms("net8.0")
    # exact first, then descending fallbacks
    assert chain[0] == "net8.0"
    assert "netstandard2.1" in chain
    assert "netstandard2.0" in chain
    # order preserved between net5.0 and netstandard2.1
    assert chain.index("netstandard2.1") < chain.index("netstandard2.0")


def test_multitarget_deduped_and_concatenated() -> None:
    chain = _candidate_tfms("net6.0;net8.0")
    # net6.0 fallback comes first then net8.0 fallback
    assert chain.index("net6.0") < chain.index("net8.0")
    # netstandard2.0 should appear exactly once
    assert chain.count("netstandard2.0") == 1


def test_none_tfm_uses_safe_defaults() -> None:
    chain = _candidate_tfms(None)
    assert "netstandard2.0" in chain


# --------------------------------------------------------------- version sort


def test_version_sort_orders_numerically() -> None:
    versions = ["1.0.0", "10.0.0", "2.0.0", "1.2.3"]
    versions.sort(key=_version_sort_key, reverse=True)
    assert versions[0] == "10.0.0"
    assert versions[-1] == "1.0.0"


def test_version_sort_handles_prerelease() -> None:
    # Numeric prefix wins; "-beta" suffix is ignored.
    assert _version_sort_key("1.2.3-beta") == (1, 2, 3, 0)


# --------------------------------------------------------------- NuGet cache helpers


def _seed_nuget(
    cache: Path, *, pkg: str, version: str, tfm: str, assembly: str
) -> Path:
    """Create a fake `~/.nuget/packages/pkg/version/lib/tfm/Assembly.dll`."""
    d = cache / pkg.lower() / version / "lib" / tfm
    d.mkdir(parents=True)
    dll = d / f"{assembly}.dll"
    dll.write_bytes(b"MZ")  # not a real PE; resolver only cares about presence
    return dll


def test_resolve_exact_version_exact_tfm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NUGET_PACKAGES", str(tmp_path))
    dll = _seed_nuget(
        tmp_path,
        pkg="Newtonsoft.Json",
        version="13.0.3",
        tfm="net8.0",
        assembly="Newtonsoft.Json",
    )
    out = resolve_package_dlls(PackageRef("Newtonsoft.Json", "13.0.3"), "net8.0")
    assert out == [dll]


def test_resolve_falls_back_through_tfm_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only netstandard2.0 on disk — net8.0 caller must still find it."""
    monkeypatch.setenv("NUGET_PACKAGES", str(tmp_path))
    dll = _seed_nuget(
        tmp_path, pkg="Foo", version="1.0.0", tfm="netstandard2.0", assembly="Foo"
    )
    out = resolve_package_dlls(PackageRef("Foo", "1.0.0"), "net8.0")
    assert out == [dll]


def test_resolve_picks_newest_when_version_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NUGET_PACKAGES", str(tmp_path))
    _seed_nuget(tmp_path, pkg="Foo", version="1.0.0", tfm="net8.0", assembly="Foo")
    new = _seed_nuget(
        tmp_path, pkg="Foo", version="2.5.0", tfm="net8.0", assembly="Foo"
    )
    out = resolve_package_dlls(PackageRef("Foo", None), "net8.0")
    assert out == [new]


def test_resolve_returns_empty_when_pkg_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NUGET_PACKAGES", str(tmp_path))
    assert resolve_package_dlls(PackageRef("Missing", "1.0.0"), "net8.0") == []


def test_resolve_uses_flat_lib_for_legacy_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-TFM-era packages drop DLLs straight into ``lib/`` with no subdir."""
    monkeypatch.setenv("NUGET_PACKAGES", str(tmp_path))
    d = tmp_path / "legacy" / "1.0.0" / "lib"
    d.mkdir(parents=True)
    dll = d / "Legacy.dll"
    dll.write_bytes(b"MZ")
    out = resolve_package_dlls(PackageRef("Legacy", "1.0.0"), "net8.0")
    assert dll in out


# --------------------------------------------------------------- project refs


def test_resolve_project_reference_prefers_debug(tmp_path: Path) -> None:
    proj_dir = tmp_path / "Acme.Core"
    proj_dir.mkdir()
    csproj = proj_dir / "Acme.Core.csproj"
    csproj.write_text("<Project Sdk='Microsoft.NET.Sdk' />")

    debug_dir = proj_dir / "bin" / "Debug" / "net8.0"
    debug_dir.mkdir(parents=True)
    debug_dll = debug_dir / "Acme.Core.dll"
    debug_dll.write_bytes(b"MZ")

    release_dir = proj_dir / "bin" / "Release" / "net8.0"
    release_dir.mkdir(parents=True)
    (release_dir / "Acme.Core.dll").write_bytes(b"MZ")

    out = resolve_project_reference_dlls(csproj, "net8.0")
    assert out == [debug_dll]


def test_resolve_project_reference_falls_back_to_flat_bin(tmp_path: Path) -> None:
    proj_dir = tmp_path / "Legacy"
    proj_dir.mkdir()
    csproj = proj_dir / "Legacy.csproj"
    csproj.write_text("<Project ToolsVersion='15.0' />")
    bin_dir = proj_dir / "bin"
    bin_dir.mkdir()
    dll = bin_dir / "Legacy.dll"
    dll.write_bytes(b"MZ")
    out = resolve_project_reference_dlls(csproj, None)
    assert dll in out


def test_resolve_project_reference_empty_when_no_output(tmp_path: Path) -> None:
    proj_dir = tmp_path / "NotBuilt"
    proj_dir.mkdir()
    csproj = proj_dir / "NotBuilt.csproj"
    csproj.write_text("<Project Sdk='Microsoft.NET.Sdk' />")
    assert resolve_project_reference_dlls(csproj, "net8.0") == []


# --------------------------------------------------------------- resolve_refs


def test_resolve_refs_combines_packages_and_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nuget = tmp_path / "nuget"
    monkeypatch.setenv("NUGET_PACKAGES", str(nuget))
    _seed_nuget(
        nuget,
        pkg="Newtonsoft.Json",
        version="13.0.3",
        tfm="net8.0",
        assembly="Newtonsoft.Json",
    )

    ref_proj = tmp_path / "Acme.Core" / "Acme.Core.csproj"
    ref_proj.parent.mkdir()
    ref_proj.write_text("<Project Sdk='Microsoft.NET.Sdk' />")
    (ref_proj.parent / "bin" / "Debug" / "net8.0").mkdir(parents=True)
    out_dll = ref_proj.parent / "bin" / "Debug" / "net8.0" / "Acme.Core.dll"
    out_dll.write_bytes(b"MZ")

    info = CsprojInfo(
        path=str(tmp_path / "Acme.App.csproj"),
        name="Acme.App",
        target_framework="net8.0",
        project_references=[str(ref_proj)],
        package_references=[PackageRef("Newtonsoft.Json", "13.0.3")],
    )

    refs = resolve_refs(info)
    assert "Newtonsoft.Json" in refs.package_dlls
    assert str(ref_proj) in refs.project_dlls
    assert out_dll in refs.project_dlls[str(ref_proj)]
    # all_paths flattens
    assert out_dll in refs.all_paths()
