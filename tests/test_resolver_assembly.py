"""Tests for the assembly-exposed resolver tier.

Reuses the fake graph from ``test_resolver`` to drive the new path
that resolves unresolved calls in .NET source against ``Type`` nodes
on assemblies referenced by the calling file's project.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow importing sibling test helpers without packaging the tests dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_memory.orchestrator.resolver import (  # noqa: E402
    PLACEHOLDER_PREFIX,
    resolve_graph,
)
from test_resolver import _FakeGraph, _FakeStore  # noqa: E402


# --------------------------------------------------------------- helpers


JSON_PH = f"{PLACEHOLDER_PREFIX}JsonConvert"
NEWTONSOFT_KEY = "Newtonsoft.Json, Version=13.0.3.0"
NEWTONSOFT_TYPE_KEY = f"{NEWTONSOFT_KEY}::Newtonsoft.Json.JsonConvert"


def _store(**kw: object) -> _FakeStore:
    """Build a fake store with sensible empty defaults."""
    defaults: dict[str, object] = {
        "defines": [],
        "imports": [],
        "placeholders": [],
        "calls": [],
        "file_project": [],
        "project_assemblies": [],
        "type_index": [],
    }
    defaults.update(kw)
    return _FakeStore(_FakeGraph(**defaults))  # type: ignore[arg-type]


# --------------------------------------------------------------- resolution


def test_unique_type_in_referenced_assembly_resolves() -> None:
    store = _store(
        placeholders=[(JSON_PH, "JsonConvert")],
        calls=[("/repo/Acme/App.cs", JSON_PH)],
        file_project=[("/repo/Acme/App.cs", "/repo/Acme/Acme.csproj")],
        project_assemblies=[("/repo/Acme/Acme.csproj", NEWTONSOFT_KEY)],
        type_index=[("JsonConvert", NEWTONSOFT_TYPE_KEY, NEWTONSOFT_KEY)],
    )
    stats = resolve_graph(store)
    assert stats.edges_resolved_assembly == 1
    assert stats.edges_left_external == 0
    # writeback used the Type-target query batch.
    rewrites = [w for w in store.graph.writes if w[0] == "rewrite"]
    assert len(rewrites) == 1
    payload = rewrites[0][1]
    assert payload["target"] == NEWTONSOFT_TYPE_KEY
    assert payload["conf"] == "external"


def test_type_not_resolved_when_assembly_not_referenced() -> None:
    """Type exists in the index but the file's project doesn't reference it."""
    other_asm = "Some.Other, Version=1.0.0.0"
    other_type = f"{other_asm}::Some.Other.JsonConvert"
    store = _store(
        placeholders=[(JSON_PH, "JsonConvert")],
        calls=[("/repo/Acme/App.cs", JSON_PH)],
        file_project=[("/repo/Acme/App.cs", "/repo/Acme/Acme.csproj")],
        project_assemblies=[("/repo/Acme/Acme.csproj", NEWTONSOFT_KEY)],
        type_index=[("JsonConvert", other_type, other_asm)],
    )
    stats = resolve_graph(store)
    assert stats.edges_resolved_assembly == 0
    assert stats.edges_left_external == 1


def test_ambiguous_when_multiple_referenced_assemblies_expose_same_name() -> None:
    """Two referenced assemblies both expose ``Path`` → bail rather than guess."""
    asm_a = "Acme.PathLib, Version=1.0.0.0"
    asm_b = "Acme.PathOther, Version=1.0.0.0"
    store = _store(
        placeholders=[(f"{PLACEHOLDER_PREFIX}Path", "Path")],
        calls=[("/repo/A/App.cs", f"{PLACEHOLDER_PREFIX}Path")],
        file_project=[("/repo/A/App.cs", "/repo/A/A.csproj")],
        project_assemblies=[
            ("/repo/A/A.csproj", asm_a),
            ("/repo/A/A.csproj", asm_b),
        ],
        type_index=[
            ("Path", f"{asm_a}::Acme.PathLib.Path", asm_a),
            ("Path", f"{asm_b}::Acme.PathOther.Path", asm_b),
        ],
    )
    stats = resolve_graph(store)
    assert stats.edges_resolved_assembly == 0
    assert stats.edges_left_external == 1


def test_file_without_project_containment_falls_through_to_external() -> None:
    """Non-.NET files don't get CONTAINED_IN edges → can't use this tier."""
    store = _store(
        placeholders=[(JSON_PH, "JsonConvert")],
        calls=[("/repo/web/app.ts", JSON_PH)],
        # no file_project mapping for .ts file
        project_assemblies=[("/repo/Acme/Acme.csproj", NEWTONSOFT_KEY)],
        type_index=[("JsonConvert", NEWTONSOFT_TYPE_KEY, NEWTONSOFT_KEY)],
    )
    stats = resolve_graph(store)
    assert stats.edges_resolved_assembly == 0
    assert stats.edges_left_external == 1


def test_same_file_resolution_wins_over_assembly() -> None:
    """If the in-project source defines X, the resolver picks that first."""
    store = _store(
        defines=[("/repo/Acme/App.cs", "JsonConvert", "/repo/Acme/App.cs::JsonConvert#10")],
        placeholders=[(JSON_PH, "JsonConvert")],
        calls=[("/repo/Acme/App.cs", JSON_PH)],
        file_project=[("/repo/Acme/App.cs", "/repo/Acme/Acme.csproj")],
        project_assemblies=[("/repo/Acme/Acme.csproj", NEWTONSOFT_KEY)],
        type_index=[("JsonConvert", NEWTONSOFT_TYPE_KEY, NEWTONSOFT_KEY)],
    )
    stats = resolve_graph(store)
    assert stats.edges_resolved_same_file == 1
    assert stats.edges_resolved_assembly == 0


def test_resolver_stats_expose_assembly_counter() -> None:
    """The new counter must surface on ``ResolverStats``."""
    store = _store(
        placeholders=[(JSON_PH, "JsonConvert")],
        calls=[("/repo/Acme/App.cs", JSON_PH)],
        file_project=[("/repo/Acme/App.cs", "/repo/Acme/Acme.csproj")],
        project_assemblies=[("/repo/Acme/Acme.csproj", NEWTONSOFT_KEY)],
        type_index=[("JsonConvert", NEWTONSOFT_TYPE_KEY, NEWTONSOFT_KEY)],
    )
    stats = resolve_graph(store)
    assert hasattr(stats, "edges_resolved_assembly")
    assert stats.edges_resolved_assembly == 1
