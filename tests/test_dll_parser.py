"""DLL metadata parser tests.

We can't easily ship a known DLL in the repo (binary, license-tricky),
so most tests build a fake assembly in memory by patching the dnfile
surface. The one integration check looks for a real DLL alongside the
test repo and skips when absent — that path runs locally for the
maintainer, never in CI.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from code_memory.extractor import dll as dll_mod
from code_memory.extractor.dll import (
    AssemblyInfo,
    TypeRef,
    _classify_type,
    _typedef_to_ref,
    parse_assembly,
)


# --------------------------------------------------------------- TypeRef + identity


def test_assemblyinfo_identity_uses_name_and_version() -> None:
    info = AssemblyInfo(path="/x.dll", name="Foo.Bar", version="1.2.3.4")
    assert info.identity == "Foo.Bar, Version=1.2.3.4"


def test_typeref_is_immutable() -> None:
    t = TypeRef(namespace="N", name="C", kind="class")
    with pytest.raises(Exception):
        t.name = "Other"  # type: ignore[misc]


# --------------------------------------------------------------- _classify_type


def _flags(**kw: bool) -> SimpleNamespace:
    base = dict(
        tdPublic=True,
        tdNestedPublic=False,
        tdInterface=False,
        tdSealed=False,
        tdSequentialLayout=False,
        tdExplicitLayout=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_classify_interface() -> None:
    assert _classify_type(_flags(tdInterface=True)) == "interface"


def test_classify_struct_via_layout() -> None:
    assert _classify_type(_flags(tdSequentialLayout=True)) == "struct"
    assert _classify_type(_flags(tdExplicitLayout=True)) == "struct"


def test_classify_defaults_to_class() -> None:
    assert _classify_type(_flags()) == "class"


# --------------------------------------------------------------- _typedef_to_ref


def _td_row(
    namespace: str, name: str, **flag_kw: bool
) -> SimpleNamespace:
    return SimpleNamespace(
        TypeNamespace=namespace,
        TypeName=name,
        Flags=_flags(**flag_kw),
    )


def test_typedef_module_pseudotype_dropped() -> None:
    assert _typedef_to_ref(_td_row("", "<Module>")) is None


def test_typedef_private_dropped() -> None:
    # default is tdPublic=True; flip both visibility flags off.
    assert (
        _typedef_to_ref(_td_row("N", "C", tdPublic=False, tdNestedPublic=False))
        is None
    )


def test_typedef_public_kept() -> None:
    ref = _typedef_to_ref(_td_row("Acme.Lib", "Greeter", tdPublic=True))
    assert ref == TypeRef(namespace="Acme.Lib", name="Greeter", kind="class", sealed=False)


def test_typedef_sealed_flag_propagated() -> None:
    ref = _typedef_to_ref(_td_row("N", "C", tdPublic=True, tdSealed=True))
    assert ref is not None and ref.sealed is True


def test_typedef_nested_public_kept() -> None:
    ref = _typedef_to_ref(
        _td_row("N", "Outer.Inner", tdPublic=False, tdNestedPublic=True)
    )
    assert ref is not None and ref.name == "Outer.Inner"


# --------------------------------------------------------------- parse_assembly: failure paths


def test_parse_nonexistent_returns_none(tmp_path: Path) -> None:
    assert parse_assembly(tmp_path / "missing.dll") is None


def test_parse_non_pe_file_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "fake.dll"
    f.write_bytes(b"hello world, not a PE file")
    assert parse_assembly(f) is None


def test_parse_missing_dnfile_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the optional ``[dotnet]`` extra isn't installed, parse is a no-op."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kw: object) -> object:
        if name == "dnfile":
            raise ImportError("dnfile not installed")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert parse_assembly("/tmp/anything.dll") is None


# --------------------------------------------------------------- parse_assembly: success path (real DLL, optional)


_LOCAL_DLL = Path(
    "/Users/fmflurry/Workspace/internal/private-monorepo/PC/DotNet/Sources/bin/Debug/"
    "netstandard2.0/Acme.Common.Rules.dll"
)


@pytest.mark.skipif(
    not _LOCAL_DLL.exists(),
    reason="real .NET DLL not present on this host",
)
def test_parse_real_assembly_locally() -> None:
    info = parse_assembly(_LOCAL_DLL)
    assert info is not None
    assert info.name == "Acme.Common.Rules"
    # version is shaped Major.Minor.Build.Revision
    assert info.version.count(".") == 3
    # Should expose public types — exact count drifts with the source.
    assert len(info.types) > 10
    namespaces = {t.namespace for t in info.types}
    assert any(ns.startswith("Acme.GC") for ns in namespaces)


# --------------------------------------------------------------- walk_dlls


def test_walk_dlls_drops_failures(tmp_path: Path) -> None:
    junk = tmp_path / "garbage.dll"
    junk.write_bytes(b"not a PE")
    out = dll_mod.walk_dlls([junk])
    assert out == []
