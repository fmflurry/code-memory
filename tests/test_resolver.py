"""Tests for the post-ingest symbol resolver.

These tests don't need a live FalkorDB. They stub the ``graph.query``
interface with a minimal fixture that returns canned result sets and
records writes for assertion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from code_memory.extractor.treesitter import CALLEE_STOPLIST, _last_identifier
from code_memory.orchestrator.resolver import (
    PLACEHOLDER_PREFIX,
    _resolve_relative_import,
    resolve_graph,
)


# ---------------------------------------------------------------- helpers


@dataclass
class _QueryResult:
    result_set: list[list[Any]]
    nodes_deleted: int = 0


class _FakeGraph:
    """In-memory stand-in for FalkorDB's Graph object."""

    def __init__(
        self,
        defines: list[tuple[str, str, str]],  # (file, sym_name, sym_key)
        imports: list[tuple[str, str]],  # (file, module_key)
        placeholders: list[tuple[str, str]],  # (placeholder_key, sym_name)
        calls: list[tuple[str, str]],  # (file, placeholder_key)
        *,
        file_project: list[tuple[str, str]] | None = None,  # (file, project_key)
        project_assemblies: list[tuple[str, str]] | None = None,  # (project_key, asm_key)
        type_index: list[tuple[str, str, str]] | None = None,  # (type_name, type_key, asm_key)
        inject_edges: list[tuple[str, str]] | None = None,  # (file, placeholder_key)
        reference_edges: list[tuple[str, str]] | None = None,  # (file, placeholder_key)
    ) -> None:
        self.defines = list(defines)
        self.imports = list(imports)
        self.placeholders = list(placeholders)
        self.calls = list(calls)
        self.file_project = list(file_project or [])
        self.project_assemblies = list(project_assemblies or [])
        self.type_index = list(type_index or [])
        self.inject_edges = list(inject_edges or [])
        self.reference_edges = list(reference_edges or [])
        self.writes: list[tuple[str, dict[str, Any]]] = []

    def query(self, q: str, params: dict[str, Any] | None = None) -> _QueryResult:
        params = params or {}
        # Read paths — match by structural keywords.
        if "MATCH (f:File)-[:DEFINES]->(s:Symbol)" in q and "s.name" in q and "s.key" in q:
            # New query shape includes s.params; pad with None when
            # tests didn't supply a fourth column.
            return _QueryResult(
                [
                    list(r) + ([None] if len(r) == 3 else [])
                    for r in self.defines
                ]
            )
        if "MATCH (f:File)-[:IMPORTS]->(m:Module)" in q:
            return _QueryResult([list(r) for r in self.imports])
        if "STARTS WITH $p RETURN s.key, s.name" in q:
            return _QueryResult([list(r) for r in self.placeholders])
        if "(f:File)-[r:CALLS]->(s:Symbol)" in q and "STARTS WITH $p" in q:
            # Tests supply (file, placeholder) pairs; the resolver
            # asks for arity too — pad with -1 for "unknown arity".
            return _QueryResult([list(r) + [-1] for r in self.calls])
        if "[:CONTAINED_IN]" in q:
            return _QueryResult([list(r) for r in self.file_project])
        if "[:USES_ASSEMBLY]" in q:
            return _QueryResult([list(r) for r in self.project_assemblies])
        if "[:EXPOSES_TYPE]" in q:
            return _QueryResult([list(r) for r in self.type_index])
        if (
            "(f:File)-[:INJECTS]" in q
            and "STARTS WITH $p" in q
            and "RETURN f.key, s.key" in q
        ):
            return _QueryResult([list(r) for r in self.inject_edges])
        if (
            "(f:File)-[:REFERENCES]" in q
            and "STARTS WITH $p" in q
            and "RETURN f.key, s.key" in q
        ):
            return _QueryResult([list(r) for r in self.reference_edges])

        # Write paths — apply the rewrite to the in-memory tables.
        if "UNWIND $rows" in q and "MERGE (f)-[r:CALLS]->(t)" in q:
            for row in params["rows"]:
                self.writes.append(("rewrite", row))
                self.calls = [
                    e for e in self.calls
                    if not (e[0] == row["file"] and e[1] == row["placeholder"])
                ]
            return _QueryResult([])
        if "UNWIND $rows" in q and "MERGE (f)-[r:INJECTS]->(t)" in q:
            for row in params["rows"]:
                self.writes.append(("rewrite_inject", row))
                self.inject_edges = [
                    e for e in self.inject_edges
                    if not (e[0] == row["file"] and e[1] == row["placeholder"])
                ]
            return _QueryResult([])
        if "UNWIND $rows" in q and "MERGE (f)-[r:REFERENCES]->(t)" in q:
            for row in params["rows"]:
                self.writes.append(("rewrite_reference", row))
                self.reference_edges = [
                    e for e in self.reference_edges
                    if not (e[0] == row["file"] and e[1] == row["placeholder"])
                ]
            return _QueryResult([])
        if "UNWIND $rows" in q and "MERGE (m:Module" in q and "IMPORTS" in q:
            for row in params["rows"]:
                self.writes.append(("alias_import", row))
            return _QueryResult([])
        if "NOT ( ()-[:CALLS]->(s) )" in q:
            referenced = (
                {e[1] for e in self.calls}
                | {e[1] for e in self.inject_edges}
                | {e[1] for e in self.reference_edges}
            )
            deleted = 0
            new_placeholders = []
            for key, name in self.placeholders:
                if key not in referenced:
                    deleted += 1
                    self.writes.append(("delete", {"key": key}))
                else:
                    new_placeholders.append((key, name))
            self.placeholders = new_placeholders
            return _QueryResult([], nodes_deleted=deleted)

        raise AssertionError(f"unexpected query: {q[:80]}")


class _FakeStore:
    def __init__(self, fake: _FakeGraph) -> None:
        self.graph = fake


# ---------------------------------------------------------------- callee normalization


def test_last_identifier_handles_member_expression() -> None:
    assert _last_identifier("this.svc.getBearerToken") == "getBearerToken"
    assert _last_identifier("MyClass.staticFn") == "staticFn"
    assert _last_identifier("foo") == "foo"


def test_last_identifier_rejects_computed_and_calls() -> None:
    assert _last_identifier("arr[i]") is None
    assert _last_identifier("foo()") is None


def test_callee_stoplist_blocks_framework_noise() -> None:
    for noisy in ("inject", "Injectable", "console", "JSON", "map", "pipe", "subscribe"):
        assert noisy in CALLEE_STOPLIST


# ---------------------------------------------------------------- relative import resolution


def test_resolve_relative_import_extension_probe(tmp_path) -> None:
    project = {str(tmp_path / "a" / "bar.ts")}
    out = _resolve_relative_import(tmp_path / "a", "./bar", project)
    assert out == str(tmp_path / "a" / "bar.ts")


def test_resolve_relative_import_directory_index(tmp_path) -> None:
    project = {str(tmp_path / "a" / "bar" / "index.ts")}
    out = _resolve_relative_import(tmp_path / "a", "./bar", project)
    assert out == str(tmp_path / "a" / "bar" / "index.ts")


def test_resolve_relative_import_unknown_returns_none(tmp_path) -> None:
    out = _resolve_relative_import(tmp_path, "./missing", set())
    assert out is None


# ---------------------------------------------------------------- resolver tiers


def test_resolver_links_same_file_call() -> None:
    fake = _FakeGraph(
        defines=[("/a/svc.ts", "foo", "/a/svc.ts::foo#10")],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}foo", "foo")],
        calls=[("/a/svc.ts", f"{PLACEHOLDER_PREFIX}foo")],
    )
    stats = resolve_graph(_FakeStore(fake))
    assert stats.edges_resolved_same_file == 1
    assert fake.writes[0][0] == "rewrite"
    assert fake.writes[0][1]["target"] == "/a/svc.ts::foo#10"
    assert fake.writes[0][1]["conf"] == "high"


def test_resolver_links_imported_symbol(tmp_path) -> None:
    bar = str(tmp_path / "bar.ts")
    caller = str(tmp_path / "caller.ts")
    fake = _FakeGraph(
        defines=[(bar, "helper", f"{bar}::helper#5")],
        imports=[(caller, "./bar")],
        placeholders=[(f"{PLACEHOLDER_PREFIX}helper", "helper")],
        calls=[(caller, f"{PLACEHOLDER_PREFIX}helper")],
    )
    # Make resolver believe ``bar`` is on disk by including it in defines map
    # (project_files derives from defines).
    stats = resolve_graph(_FakeStore(fake))
    assert stats.edges_resolved_imported == 1
    assert fake.writes[0][1]["target"] == f"{bar}::helper#5"
    assert fake.writes[0][1]["conf"] == "high"


def test_resolver_falls_back_to_project_unique() -> None:
    fake = _FakeGraph(
        defines=[("/a/util.ts", "uniqueFn", "/a/util.ts::uniqueFn#3")],
        imports=[],  # no import edge from caller
        placeholders=[(f"{PLACEHOLDER_PREFIX}uniqueFn", "uniqueFn")],
        calls=[("/b/caller.ts", f"{PLACEHOLDER_PREFIX}uniqueFn")],
    )
    stats = resolve_graph(_FakeStore(fake))
    assert stats.edges_resolved_unique == 1
    assert fake.writes[0][1]["conf"] == "medium"


def test_derive_import_aliases_for_python_package(tmp_path) -> None:
    from code_memory.orchestrator.resolver import _derive_import_aliases

    # Mimic a Python package layout: pkg/sub/leaf.py with __init__.py
    # at each level.
    pkg = tmp_path / "code_memory"
    sub = pkg / "graph"
    sub.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (sub / "__init__.py").write_text("")
    target = sub / "falkor_store.py"
    target.write_text("")

    aliases = _derive_import_aliases(str(target))
    assert str(target) in aliases  # absolute path
    assert "falkor_store" in aliases  # basename stem
    assert "code_memory.graph.falkor_store" in aliases  # dotted module


def test_derive_import_aliases_skips_non_package(tmp_path) -> None:
    """No __init__.py chain → only absolute path + basename."""
    from code_memory.orchestrator.resolver import _derive_import_aliases

    loose = tmp_path / "loose.ts"
    loose.write_text("")
    aliases = _derive_import_aliases(str(loose))
    assert aliases == [str(loose), "loose"]


def test_emit_import_aliases_writes_alias_edges(tmp_path) -> None:
    """End-to-end: a relative import gets canonical IMPORTS edges."""
    pkg = tmp_path / "code_memory"
    sub = pkg / "graph"
    sub.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (sub / "__init__.py").write_text("")
    target = str(sub / "falkor_store.py")
    (sub / "falkor_store.py").write_text("")
    importer = str(pkg / "orchestrator_resolver.py")
    (pkg / "orchestrator_resolver.py").write_text("")

    # Relative import `..graph.falkor_store` from the importer.
    fake = _FakeGraph(
        defines=[(target, "FalkorStore", f"{target}::FalkorStore#1")],
        imports=[(importer, "..graph.falkor_store")],
        placeholders=[],
        calls=[],
    )
    # _resolve_relative_import probes file_dir / mod_key. For
    # ``..graph.falkor_store`` from `importer`'s parent, that's
    # `code_memory/../graph/falkor_store` — which doesn't resolve cleanly
    # via Python-style dots. Force the test to use a path-style relative
    # import that the resolver actually supports.
    fake.imports = [(importer, "./graph/falkor_store")]
    resolve_graph(_FakeStore(fake))
    alias_writes = [w for w in fake.writes if w[0] == "alias_import"]
    aliased_keys = {w[1]["alias"] for w in alias_writes}
    assert target in aliased_keys
    assert "falkor_store" in aliased_keys
    assert "code_memory.graph.falkor_store" in aliased_keys


def test_resolver_rewrites_reference_edges() -> None:
    """REFERENCES placeholders (type-position refs) resolve like CALLS.

    Regression: ``code-memory callers IFoo`` returned 0 on a C# repo
    because no graph edge connected `class X : IFoo` to `IFoo`. The
    extractor now emits REFERENCES; the resolver must rewrite them
    to point at the real defined symbol.
    """
    iface = "/a/IFoo.cs"
    impl = "/a/Foo.cs"
    fake = _FakeGraph(
        defines=[(iface, "IFoo", f"{iface}::IFoo#1")],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}IFoo", "IFoo")],
        calls=[],
        reference_edges=[(impl, f"{PLACEHOLDER_PREFIX}IFoo")],
    )
    stats = resolve_graph(_FakeStore(fake))
    assert stats.edges_resolved_unique == 1
    rewrites = [w for w in fake.writes if w[0] == "rewrite_reference"]
    assert len(rewrites) == 1
    assert rewrites[0][1]["target"] == f"{iface}::IFoo#1"


def test_resolver_keeps_reference_only_placeholder_alive() -> None:
    """Cleanup must not delete a placeholder when only REFERENCES point at it."""
    fake = _FakeGraph(
        defines=[],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}IFoo", "IFoo")],
        calls=[],
        reference_edges=[("/a/x.cs", f"{PLACEHOLDER_PREFIX}IFoo")],
    )
    resolve_graph(_FakeStore(fake))
    deletes = [w for w in fake.writes if w[0] == "delete"]
    assert deletes == []


def test_resolver_leaves_ambiguous_unresolved() -> None:
    fake = _FakeGraph(
        defines=[
            ("/a/x.ts", "handle", "/a/x.ts::handle#1"),
            ("/b/y.ts", "handle", "/b/y.ts::handle#1"),
        ],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}handle", "handle")],
        calls=[("/c/caller.ts", f"{PLACEHOLDER_PREFIX}handle")],
    )
    stats = resolve_graph(_FakeStore(fake))
    assert stats.edges_left_ambiguous == 1
    assert stats.edges_resolved_unique == 0
    # No rewrite should have happened
    assert not any(w[0] == "rewrite" for w in fake.writes)


def test_resolver_leaves_external_unresolved() -> None:
    fake = _FakeGraph(
        defines=[("/a/x.ts", "local", "/a/x.ts::local#1")],
        imports=[("/a/x.ts", "rxjs")],  # bare module, not relative
        placeholders=[(f"{PLACEHOLDER_PREFIX}from_lib", "from_lib")],
        calls=[("/a/x.ts", f"{PLACEHOLDER_PREFIX}from_lib")],
    )
    stats = resolve_graph(_FakeStore(fake))
    assert stats.edges_left_external == 1


def test_resolver_cleans_up_orphan_placeholders() -> None:
    fake = _FakeGraph(
        defines=[("/a/svc.ts", "foo", "/a/svc.ts::foo#10")],
        imports=[],
        placeholders=[
            (f"{PLACEHOLDER_PREFIX}foo", "foo"),
            (f"{PLACEHOLDER_PREFIX}bar", "bar"),  # never called by anyone
        ],
        calls=[("/a/svc.ts", f"{PLACEHOLDER_PREFIX}foo")],
    )
    stats = resolve_graph(_FakeStore(fake))
    # foo placeholder loses its CALLS edge after rewrite -> orphan -> deleted
    # bar placeholder has no edges to start -> orphan -> deleted
    assert stats.placeholders_deleted >= 1
    deletes = [w for w in fake.writes if w[0] == "delete"]
    deleted_keys = {w[1]["key"] for w in deletes}
    assert f"{PLACEHOLDER_PREFIX}bar" in deleted_keys


def test_resolver_records_full_stats() -> None:
    fake = _FakeGraph(
        defines=[("/a/x.ts", "foo", "/a/x.ts::foo#1")],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}foo", "foo")],
        calls=[("/a/x.ts", f"{PLACEHOLDER_PREFIX}foo")],
    )
    stats = resolve_graph(_FakeStore(fake))
    assert stats.placeholders == 1
    assert stats.edges_total == 1
    assert stats.edges_resolved_same_file == 1
    assert stats.edges_resolved_imported == 0
    assert stats.edges_resolved_unique == 0
