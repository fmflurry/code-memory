"""Tests for FalkorStore topology query primitives.

The methods translate user-facing intents into Cypher queries. These
tests verify the Cypher shape (no live FalkorDB needed) and that
result-set rows are decoded into the documented dict layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from code_memory.graph import FalkorStore


@dataclass
class _QueryResult:
    result_set: list[list[Any]]


class _RecordingGraph:
    def __init__(self, canned: list[list[Any]]) -> None:
        self.canned = canned
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def query(self, q: str, params: dict[str, Any] | None = None) -> _QueryResult:
        self.queries.append((q, params or {}))
        return _QueryResult(self.canned)


def _store_with(rows: list[list[Any]]) -> tuple[FalkorStore, _RecordingGraph]:
    store = FalkorStore.__new__(FalkorStore)  # bypass __init__ (avoids live connection)
    fake = _RecordingGraph(rows)
    store.graph = fake  # type: ignore[assignment]
    return store, fake


# ---------------------------------------------------------------- callers


def test_callers_returns_decoded_rows() -> None:
    store, _ = _store_with(
        [["/a/caller.ts", "/b/def.ts", 12, 30, "method_definition"]]
    )
    out = store.callers("getBearerToken")
    assert out == [
        {
            "caller": "/a/caller.ts",
            "target_file": "/b/def.ts",
            "target_start": 12,
            "target_end": 30,
            "target_kind": "method_definition",
        }
    ]


def test_callers_clamps_depth_into_range() -> None:
    """depth>1 is implemented in Python (one Cypher hop per ring), so
    instead of asserting a var-length path we count the number of
    issued queries: depth=N triggers up to N hops + DEFINES lookups."""
    store, fake = _store_with([])
    store.callers("x", depth=99)
    # No callers found → just one query, no DEFINES walk.
    assert len(fake.queries) == 1
    assert "CALLS|REFERENCES]->(s)" in fake.queries[0][0]


def test_callers_walks_defines_for_multi_hop() -> None:
    """When depth>1 and a caller exists, the store also queries DEFINES
    on that caller's file to find the next ring of targets."""

    class _Router:
        def __init__(self) -> None:
            self.queries: list[tuple[str, dict[str, Any]]] = []

        def query(self, q: str, params: dict[str, Any] | None = None) -> _QueryResult:
            self.queries.append((q, params or {}))
            if "[:DEFINES]" in q:
                return _QueryResult([["execute"]])
            return _QueryResult(
                [["/a/use-case.ts", "/b/port.ts", 5, 10, "abstract_method_signature"]]
            )

    store = FalkorStore.__new__(FalkorStore)
    router = _Router()
    store.graph = router  # type: ignore[assignment]
    store.callers("with", depth=2)
    assert len(router.queries) >= 2
    assert "[:DEFINES]" in router.queries[1][0]


def test_callers_passes_symbol_name_param() -> None:
    store, fake = _store_with([])
    store.callers("myFn")
    assert fake.queries[0][1] == {"name": "myFn"}


# ---------------------------------------------------------------- callees


def test_callees_decodes_rows_resolved() -> None:
    store, _ = _store_with(
        [["helper", "/b/util.ts", 5, 8, "function_declaration", None, "Symbol"]]
    )
    out = store.callees("doWork")
    assert out == [
        {
            "name": "helper",
            "file": "/b/util.ts",
            "start": 5,
            "end": 8,
            "kind": "function_declaration",
            "resolved": True,
            "label": "Symbol",
        }
    ]


def test_callees_surfaces_unresolved_with_flag() -> None:
    """Placeholder targets (resolver couldn't bind) are no longer hidden;
    callers see them with ``resolved=False`` so empty-callee bug
    (Angular use cases, bare method names) stops being silent."""
    store, _ = _store_with(
        [["with", None, None, None, None, True, "Symbol"]]
    )
    out = store.callees("CreateDraftUseCase")
    assert out == [
        {
            "name": "with",
            "file": None,
            "start": None,
            "end": None,
            "kind": None,
            "resolved": False,
            "label": "Symbol",
        }
    ]


def test_callees_includes_type_targets() -> None:
    """Resolver may point a CALLS edge at a ``Type`` node (constructor
    or external assembly type). Those must surface alongside Symbols."""
    store, _ = _store_with(
        [["Foo", "asm::Lib::Foo", None, None, None, None, "Type"]]
    )
    out = store.callees("doWork")
    assert out[0]["label"] == "Type"
    assert out[0]["resolved"] is True


def test_callees_depth_chains_via_python_when_above_one(
    monkeypatch: object,  # type: ignore[unused-argument]
) -> None:
    """depth>1 cannot be expressed as a Cypher var-length path because
    CALLS goes File→Symbol only (no Symbol→File reverse edge). The
    store recurses through DEFINES in Python instead."""
    store, fake = _store_with([])
    # First call returns one resolved callee; second (recursive) returns
    # the canned row again. We just assert the query was issued for
    # both the original symbol and the discovered callee's file.
    fake.canned = [["helper", "/b/util.ts", 5, 8, "function_declaration", None, "Symbol"]]
    store.callees("doWork", depth=2)
    # Two queries: one for the original symbol, one walking from the
    # defining file of the discovered callee.
    assert len(fake.queries) >= 2


# ---------------------------------------------------------------- injects


def test_injects_returns_resolved_tokens() -> None:
    """A use case's DI dependencies surface via INJECTS edges. Without
    this query the agent can't ask 'what does CreateDraftUseCase depend
    on?' — calling `dependencies` only returns imported modules, not
    Angular DI tokens."""
    store, _ = _store_with(
        [["CreatePurchaseOrderDraftPort", "/r/port.ts::Port#4", "/r/port.ts", "abstract_class_declaration", None]]
    )
    out = store.injects("CreateDraftUseCase")
    assert out == [
        {
            "name": "CreatePurchaseOrderDraftPort",
            "key": "/r/port.ts::Port#4",
            "file": "/r/port.ts",
            "kind": "abstract_class_declaration",
            "resolved": True,
        }
    ]


def test_injects_surfaces_unresolved_tokens() -> None:
    """When the DI token is an external symbol the resolver couldn't
    bind, surface it with resolved=False rather than hiding it."""
    store, _ = _store_with(
        [["ExternalToken", "name::ExternalToken", None, None, True]]
    )
    out = store.injects("MyService")
    assert out[0]["resolved"] is False
    assert out[0]["name"] == "ExternalToken"


# ---------------------------------------------------------------- injectors


def test_injectors_returns_files() -> None:
    """Reverse: who injects this token?"""
    store, _ = _store_with(
        [["/r/a/use-case.ts"], ["/r/b/use-case.ts"]]
    )
    out = store.injectors("CreatePurchaseOrderDraftPort")
    assert out == [
        {"file": "/r/a/use-case.ts"},
        {"file": "/r/b/use-case.ts"},
    ]


# ---------------------------------------------------------------- importers


def test_importers_decodes_rows() -> None:
    store, _ = _store_with(
        [
            ["/r/a.ts", "@acme-ng/security"],
            ["/r/b.ts", "@acme-ng/security"],
        ]
    )
    out = store.importers("@acme-ng/security")
    assert out == [
        {"file": "/r/a.ts", "module": "@acme-ng/security"},
        {"file": "/r/b.ts", "module": "@acme-ng/security"},
    ]


def test_importers_passes_target_param() -> None:
    store, fake = _store_with([])
    store.importers("./bar")
    assert fake.queries[0][1] == {"key": "./bar"}


# ---------------------------------------------------------------- dependencies


def test_dependencies_decodes_rows() -> None:
    store, _ = _store_with([["rxjs"], ["@angular/core"], ["./bar"]])
    out = store.dependencies("/r/caller.ts")
    assert out == [
        {"module": "rxjs"},
        {"module": "@angular/core"},
        {"module": "./bar"},
    ]


def test_dependencies_respects_depth() -> None:
    store, fake = _store_with([])
    store.dependencies("/r/f.ts", depth=2)
    assert "IMPORTS*1..2" in fake.queries[0][0]


# ---------------------------------------------------------------- definitions


def test_definitions_decodes_rows() -> None:
    store, _ = _store_with(
        [
            ["/r/a.ts", 1, 20, "class_declaration"],
            ["/r/b.ts", 5, 12, "function_declaration"],
        ]
    )
    out = store.definitions("AuthService")
    assert out == [
        {"file": "/r/a.ts", "start": 1, "end": 20, "kind": "class_declaration"},
        {
            "file": "/r/b.ts",
            "start": 5,
            "end": 12,
            "kind": "function_declaration",
        },
    ]


def test_definitions_excludes_placeholder_symbols() -> None:
    store, fake = _store_with([])
    store.definitions("x")
    assert "s.unresolved IS NULL" in fake.queries[0][0]
