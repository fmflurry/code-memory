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
    store, fake = _store_with([])
    store.callers("x", depth=99)
    assert "CALLS*1..3" in fake.queries[0][0]
    store.callers("x", depth=0)
    assert "CALLS*1..1" in fake.queries[1][0]


def test_callers_passes_symbol_name_param() -> None:
    store, fake = _store_with([])
    store.callers("myFn")
    assert fake.queries[0][1] == {"name": "myFn"}


# ---------------------------------------------------------------- callees


def test_callees_decodes_rows() -> None:
    store, _ = _store_with(
        [["helper", "/b/util.ts", 5, 8, "function_declaration"]]
    )
    out = store.callees("doWork")
    assert out == [
        {
            "name": "helper",
            "file": "/b/util.ts",
            "start": 5,
            "end": 8,
            "kind": "function_declaration",
        }
    ]


def test_callees_excludes_unresolved_targets() -> None:
    store, fake = _store_with([])
    store.callees("doWork")
    assert "target.unresolved IS NULL" in fake.queries[0][0]


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
