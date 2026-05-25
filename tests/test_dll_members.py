"""Tests for member-level DLL inspection."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from code_memory.extractor.dll import (
    MemberRef,
    _members_for_type,
    _method_param_count,
    parse_type_members,
)


# --------------------------------------------------------------- MemberRef


def test_memberref_is_immutable() -> None:
    m = MemberRef(name="Run", kind="method", static=False, params=2)
    with pytest.raises(Exception):
        m.name = "Other"  # type: ignore[misc]


# --------------------------------------------------------------- _method_param_count


def test_param_count_uses_paramlist_length() -> None:
    row = SimpleNamespace(ParamList=[object(), object(), object()])
    assert _method_param_count(row) == 3


def test_param_count_falls_back_to_zero() -> None:
    row = SimpleNamespace(ParamList=None)
    assert _method_param_count(row) == 0


def test_param_count_handles_non_iterable() -> None:
    row = SimpleNamespace(ParamList=42)
    assert _method_param_count(row) == 0


# --------------------------------------------------------------- _members_for_type


def _method(
    name: str, *, params: int = 0, public: bool = True, static: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        Name=name,
        Flags=SimpleNamespace(mdPublic=public, mdStatic=static),
        ParamList=[object()] * params,
    )


class _StaticTable:
    """Stand-in for a dnfile MDTable that returns rows by 1-based index."""

    def __init__(self, rows: list[object]) -> None:
        self.rows = rows


def _idx(table: _StaticTable, row_index: int) -> SimpleNamespace:
    return SimpleNamespace(table=table, row_index=row_index)


def test_extracts_only_public_methods() -> None:
    methods = [
        _method("PublicOne"),
        _method("Private", public=False),
        _method("PublicTwo", static=True),
    ]
    table = _StaticTable(methods)
    row = SimpleNamespace(MethodList=[_idx(table, 1), _idx(table, 2), _idx(table, 3)])
    out = _members_for_type(None, row, 0)
    names = sorted(m.name for m in out)
    assert names == ["PublicOne", "PublicTwo"]


def test_classifies_constructors() -> None:
    methods = [_method(".ctor"), _method(".cctor"), _method("DoThing")]
    table = _StaticTable(methods)
    row = SimpleNamespace(MethodList=[_idx(table, i) for i in (1, 2, 3)])
    out = _members_for_type(None, row, 0)
    kinds = {m.name: m.kind for m in out}
    assert kinds[".ctor"] == "constructor"
    assert kinds[".cctor"] == "constructor"
    assert kinds["DoThing"] == "method"


def test_deduplicates_overloads_with_same_signature() -> None:
    """Two MethodDef rows for ``Run()`` (e.g. partial class edge case) collapse."""
    methods = [_method("Run"), _method("Run")]
    table = _StaticTable(methods)
    row = SimpleNamespace(MethodList=[_idx(table, 1), _idx(table, 2)])
    out = _members_for_type(None, row, 0)
    assert len(out) == 1


def test_keeps_real_overloads_by_param_count() -> None:
    methods = [_method("Run", params=0), _method("Run", params=2)]
    table = _StaticTable(methods)
    row = SimpleNamespace(MethodList=[_idx(table, 1), _idx(table, 2)])
    out = _members_for_type(None, row, 0)
    assert {m.params for m in out} == {0, 2}


def test_empty_method_list_returns_empty() -> None:
    row = SimpleNamespace(MethodList=[])
    assert _members_for_type(None, row, 0) == []


# --------------------------------------------------------------- parse_type_members (integration)


def test_parse_missing_dll_returns_none(tmp_path: Path) -> None:
    assert parse_type_members(tmp_path / "missing.dll", "Foo", "Bar") is None


def test_parse_non_pe_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "fake.dll"
    f.write_bytes(b"not a PE")
    assert parse_type_members(f, "Foo", "Bar") is None


import os

# Path to a real .NET DLL on the local machine + the namespace/type
# inside it. Test auto-skips when either is missing. Configure via
# ``CODEMEMORY_TEST_DLL``, ``CODEMEMORY_TEST_DLL_NAMESPACE``,
# ``CODEMEMORY_TEST_DLL_TYPE`` to point at a real assembly you own.
_LOCAL_DLL = Path(os.environ.get("CODEMEMORY_TEST_DLL", "/nonexistent.dll"))
_LOCAL_DLL_NS = os.environ.get("CODEMEMORY_TEST_DLL_NAMESPACE", "")
_LOCAL_DLL_TYPE = os.environ.get("CODEMEMORY_TEST_DLL_TYPE", "")


@pytest.mark.skipif(
    not _LOCAL_DLL.exists() or not _LOCAL_DLL_NS or not _LOCAL_DLL_TYPE,
    reason="real .NET DLL + namespace/type not configured on this host",
)
def test_parse_real_assembly_lists_methods_and_overloads() -> None:
    members = parse_type_members(
        _LOCAL_DLL, _LOCAL_DLL_NS, _LOCAL_DLL_TYPE
    )
    assert members is not None
    assert len(members) > 0
    # Should preserve at least one overload pair (same name, different arity).
    by_name: dict[str, set[int]] = {}
    for m in members:
        by_name.setdefault(m.name, set()).add(m.params)
    overloaded = {n: arities for n, arities in by_name.items() if len(arities) > 1}
    assert overloaded, "expected at least one overloaded method in the real DLL"
