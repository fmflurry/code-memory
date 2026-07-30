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
        defines: list[tuple[Any, ...]],
        # Each row is (file, sym_name, sym_key[, params[, namespace]]).
        # ``params`` and ``namespace`` are optional trailing columns —
        # tests that don't care about arity/namespace disambiguation can
        # keep supplying bare 3-tuples; FIX 2 tests supply the full
        # 5-tuple to exercise namespace-aware resolution.
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
            # Query shape is (f.key, s.name, s.key, s.params, s.namespace) —
            # 5 columns since FIX 2 (namespace-aware resolution). Pad
            # whatever-length tuple a test supplied (3, 4, or 5) up to 5
            # with None so older 3-/4-tuple fixtures keep working
            # unchanged.
            rows = []
            for r in self.defines:
                row = list(r)
                while len(row) < 5:
                    row.append(None)
                rows.append(row)
            return _QueryResult(rows)
        if "MATCH (f:File)-[:IMPORTS]->(m:Module)" in q:
            return _QueryResult([list(r) for r in self.imports])
        if "STARTS WITH $p RETURN s.key, s.name" in q:
            return _QueryResult([list(r) for r in self.placeholders])
        if "(f:File)-[r:CALLS]->(s:Symbol)" in q and "STARTS WITH $p" in q:
            # Tests supply (file, placeholder) pairs; the resolver
            # asks for arity + receiver_type — pad with defaults so
            # legacy fixtures don't need to grow extra columns.
            return _QueryResult(
                [
                    list(r) + ([-1] if len(r) == 2 else []) + ([None] if len(r) <= 3 else [])
                    for r in self.calls
                ]
            )
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


# ---------------------------------------------------------------- receiver-type narrowing


def test_resolver_narrows_by_receiver_type() -> None:
    """``this.<field>.<method>()`` resolves to the method defined in the
    same file as ``<field>``'s type, even when many other files define
    a method with the same name."""
    port_file = "/r/port.ts"
    other_file = "/r/other-with.ts"
    use_case_file = "/r/use-case.ts"
    fake = _FakeGraph(
        defines=[
            (port_file, "MyPort", f"{port_file}::MyPort#3"),
            (port_file, "with", f"{port_file}::with#5"),
            (other_file, "with", f"{other_file}::with#10"),
            (use_case_file, "UseCase", f"{use_case_file}::UseCase#2"),
        ],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}with", "with")],
        calls=[],  # filled below; we need receiver_type so use raw tuple
    )
    # Override calls with receiver_type column.
    fake.calls = [(use_case_file, f"{PLACEHOLDER_PREFIX}with", -1, "MyPort")]
    stats = resolve_graph(_FakeStore(fake))
    assert stats.edges_resolved_unique == 1
    rewrites = [w for w in fake.writes if w[0] == "rewrite"]
    assert rewrites[0][1]["target"] == f"{port_file}::with#5"


def test_resolver_falls_back_when_receiver_type_unknown() -> None:
    """When the receiver type isn't in the graph, narrowing skips and
    the next tier (project-unique / arity) runs."""
    fake = _FakeGraph(
        defines=[("/r/only.ts", "doIt", "/r/only.ts::doIt#3")],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}doIt", "doIt")],
        calls=[],
    )
    fake.calls = [("/r/use-case.ts", f"{PLACEHOLDER_PREFIX}doIt", -1, "MissingType")]
    stats = resolve_graph(_FakeStore(fake))
    # Single project-unique candidate → resolves at the unique tier.
    assert stats.edges_resolved_unique == 1


# --------------------------------------------------------- FIX 2: namespace-
# qualified resolution ("BC A's Invoice vs BC B's Invoice")
#
# Two verified failure modes (see resolver.py / pipeline.py / treesitter.py
# for the mechanics, confirmed by direct source inspection):
#
# (a) SILENT INVISIBILITY. A C# file referencing ``Invoice`` emits
#     ``REFERENCES -> name::Invoice`` — one globally shared placeholder
#     (pipeline.py's single ``dst_key=f"name::{ref}"`` emission site).
#     Tier1 (same-file) misses. Tier2 (imported) can never fire for C#:
#     ``_imported_symbols`` only follows ``kind == "relative"`` imports,
#     and a C# ``using X.Y;`` directive is classified ``"bare"`` by the
#     generic ``m_key.startswith(".")`` rule in ``_GraphState.load`` —
#     confirmed by reading that classification and ``IMPORT_NODE_TYPES``
#     in treesitter.py (``using_directive`` IS captured as a Module
#     import, it's just never *followed* for symbol resolution). Tier3
#     has 2+ candidates, and REFERENCES edges are always normalised to
#     ``arity = -1`` (``_pick_by_arity`` refuses whenever ``arity < 0``)
#     -> permanently ambiguous -> ``_callers_one_hop`` filters
#     ``unresolved IS NULL`` -> invisible.
#
# (b) GENUINE CROSS-BC MIS-BIND. ``constructor_declaration`` IS in
#     ``_CALLABLE_KINDS`` (treesitter.py), so each BC's ``Invoice`` ctor
#     is its own Symbol with a real ``param_count``. A ``CALLS`` edge
#     from BcB for ``new Invoice(x, y)`` carries ``arity=2``; if BcA's
#     ctor happens to be the ONLY project-wide arity=2 candidate,
#     ``_pick_by_arity`` picks it — silently binding to the WRONG BC's
#     constructor, confidence "medium", no warning.
#
# NOTE on the review's ``_pick_target`` "dead code" claim: verified TRUE
# by inspection. ``_pick_target`` has exactly one call site (tier 1,
# ``resolver.py`` same-file branch), reached only when
# ``name in state.file_defines[file_path]`` already holds — so
# ``same_file`` is ALWAYS non-empty there, and the "first remaining
# candidate" fallback (the ``return candidates[0][1]`` line) can never
# execute. ``_pick_target`` is NOT the mis-bind culprit; ``_pick_by_arity``
# (used by tiers 2-imported/2.5/3) is. Tests below target
# ``_pick_by_arity`` and the new namespace tier, NOT ``_pick_target`` —
# no test pins the false premise.
#
# Intended fix: ``s.namespace`` (already stored on every Symbol per
# ``pipeline.py``'s ``props["namespace"] = s.namespace``) must be
# selected by ``_GraphState.load``'s DEFINES query as a 5th column
# (``RETURN f.key, s.name, s.key, s.params, s.namespace``) and carried
# into ``name_index``. A NEW resolution tier runs BETWEEN the existing
# 2.5 (receiver-type) and 3 (project-unique): narrow the name-index
# candidates to those whose namespace matches EITHER (i) the referencing
# file's own namespace (derived from what THAT file itself defines) OR,
# if that doesn't narrow to exactly one, (ii) any entry in the file's
# raw ``file_imports`` list (regardless of "relative"/"bare" kind — a C#
# ``using`` is "bare" but its text is exactly the namespace string).
# If that narrows to exactly one candidate, bind it directly with
# confidence "high" (skip arity — a namespace-unique match is
# authoritative regardless of arity). Additionally, ``_pick_by_arity``
# must REFUSE (return None) whenever the full (pre-arity-filter)
# candidate set spans 2+ DISTINCT non-None namespaces — arity alone must
# never be trusted to pick a winner across bounded-context boundaries.
# A candidate set where every namespace is ``None`` (ordinary TS/JS,
# no namespace concept) does not count as "spanning multiple
# namespaces" — the existing arity-only disambiguation must keep
# working unchanged for non-namespaced languages.


def test_resolver_resolves_reference_via_referencing_files_own_namespace() -> None:
    """FIX 2(a) — invisibility fixed. Two BCs both define ``Invoice``;
    a REFERENCES placeholder from a file that itself lives in BcB's
    namespace must resolve to BcB's Invoice, not stay ambiguous."""
    bc_a_inv = "/BcA/Invoice.cs"
    bc_b_inv = "/BcB/Invoice.cs"
    bc_b_consumer = "/BcB/Consumer.cs"
    fake = _FakeGraph(
        defines=[
            (bc_a_inv, "Invoice", f"{bc_a_inv}::Invoice#type", None, "BcA.Domain"),
            (bc_b_inv, "Invoice", f"{bc_b_inv}::Invoice#type", None, "BcB.Domain"),
            (
                bc_b_consumer,
                "ConsumerService",
                f"{bc_b_consumer}::ConsumerService#type",
                None,
                "BcB.Domain",
            ),
        ],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}Invoice", "Invoice")],
        calls=[],
        reference_edges=[(bc_b_consumer, f"{PLACEHOLDER_PREFIX}Invoice")],
    )
    stats = resolve_graph(_FakeStore(fake))

    rewrites = [w for w in fake.writes if w[0] == "rewrite_reference"]
    assert len(rewrites) == 1, (
        f"expected exactly one REFERENCES rewrite, stats={stats}"
    )
    assert rewrites[0][1]["target"] == f"{bc_b_inv}::Invoice#type"
    assert rewrites[0][1]["conf"] == "high"
    assert stats.edges_left_ambiguous == 0


def test_resolver_prefers_namespace_match_over_cross_bc_arity_coincidence() -> None:
    """FIX 2(b) — mis-bind fixed. BcA's Invoice(2-arg) constructor is
    the ONLY project-wide arity=2 candidate, but the caller lives in
    BcB's namespace, which has its own (arity-less, e.g. record-style)
    Invoice. The namespace match must win over the coincidental
    cross-BC arity match."""
    bc_a_inv = "/BcA/Invoice.cs"
    bc_b_inv = "/BcB/Invoice.cs"
    bc_b_usecase = "/BcB/UseCase.cs"
    fake = _FakeGraph(
        defines=[
            (bc_a_inv, "Invoice", f"{bc_a_inv}::Invoice#type", None, "BcA.Domain"),
            (bc_a_inv, "Invoice", f"{bc_a_inv}::Invoice#ctor2", 2, "BcA.Domain"),
            (bc_b_inv, "Invoice", f"{bc_b_inv}::Invoice#type", None, "BcB.Domain"),
            (
                bc_b_usecase,
                "UseCase",
                f"{bc_b_usecase}::UseCase#type",
                None,
                "BcB.Domain",
            ),
        ],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}Invoice", "Invoice")],
        calls=[],
    )
    fake.calls = [(bc_b_usecase, f"{PLACEHOLDER_PREFIX}Invoice", 2, None)]
    stats = resolve_graph(_FakeStore(fake))

    rewrites = [w for w in fake.writes if w[0] == "rewrite"]
    assert len(rewrites) == 1, f"expected exactly one CALLS rewrite, stats={stats}"
    assert rewrites[0][1]["target"] == f"{bc_b_inv}::Invoice#type", (
        "must bind to BcB's own Invoice (namespace match), not BcA's "
        "arity-2 ctor which only coincidentally matched arity"
    )
    assert rewrites[0][1]["conf"] == "high"


def test_resolver_resolves_via_using_list_when_own_namespace_does_not_match() -> None:
    """FIX 2 — the "OR its using list" fallback. The referencing file's
    own namespace ("Shared.Utilities") matches neither BC, but its
    ``using BcB.Domain;`` (captured generically as a "bare" Module
    import — see ``file_imports`` — regardless of kind) uniquely
    narrows the candidates."""
    bc_a_inv = "/BcA/Invoice.cs"
    bc_b_inv = "/BcB/Invoice.cs"
    shared_consumer = "/Shared/Consumer.cs"
    fake = _FakeGraph(
        defines=[
            (bc_a_inv, "Invoice", f"{bc_a_inv}::Invoice#type", None, "BcA.Domain"),
            (bc_b_inv, "Invoice", f"{bc_b_inv}::Invoice#type", None, "BcB.Domain"),
            (
                shared_consumer,
                "ConsumerService",
                f"{shared_consumer}::ConsumerService#type",
                None,
                "Shared.Utilities",
            ),
        ],
        imports=[(shared_consumer, "BcB.Domain")],  # C# `using BcB.Domain;`
        placeholders=[(f"{PLACEHOLDER_PREFIX}Invoice", "Invoice")],
        calls=[],
        reference_edges=[(shared_consumer, f"{PLACEHOLDER_PREFIX}Invoice")],
    )
    stats = resolve_graph(_FakeStore(fake))

    rewrites = [w for w in fake.writes if w[0] == "rewrite_reference"]
    assert len(rewrites) == 1, f"expected exactly one REFERENCES rewrite, stats={stats}"
    assert rewrites[0][1]["target"] == f"{bc_b_inv}::Invoice#type"
    assert rewrites[0][1]["conf"] == "high"


def test_resolver_refuses_cross_namespace_arity_coincidence_when_own_namespace_unknown() -> (
    None
):
    """FIX 2 guard: when the caller's own namespace is unknown (a plain
    TS file, no namespace concept) AND its using-list doesn't help, AND
    the name-index candidates span multiple DISTINCT namespaces, a
    coincidental unique arity match must be REFUSED rather than
    silently trusted — staying ambiguous beats guessing across a BC
    boundary."""
    bc_a_inv = "/BcA/Invoice.cs"
    bc_b_inv = "/BcB/Invoice.cs"
    caller = "/Other/Caller.ts"
    fake = _FakeGraph(
        defines=[
            (bc_a_inv, "Invoice", f"{bc_a_inv}::Invoice#ctor2", 2, "BcA.Domain"),
            (bc_b_inv, "Invoice", f"{bc_b_inv}::Invoice#ctor3", 3, "BcB.Domain"),
        ],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}Invoice", "Invoice")],
        calls=[],
    )
    fake.calls = [(caller, f"{PLACEHOLDER_PREFIX}Invoice", 2, None)]
    stats = resolve_graph(_FakeStore(fake))

    assert stats.edges_left_ambiguous == 1, (
        f"expected the arity-2 coincidence to be refused, stats={stats}"
    )
    assert not any(w[0] == "rewrite" for w in fake.writes)


def test_resolver_arity_disambiguation_unaffected_when_no_namespace_info_present() -> (
    None
):
    """FIX 2 regression guard: ordinary non-namespaced (TS/JS) code where
    every candidate's namespace is simply ``None`` must keep resolving
    via arity exactly as before — "all None" is not "spans multiple
    namespaces". Already passes today; must still pass after the fix."""
    fake = _FakeGraph(
        defines=[
            ("/a/x.ts", "handle", "/a/x.ts::handle#1", 1, None),
            ("/b/y.ts", "handle", "/b/y.ts::handle#2", 2, None),
        ],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}handle", "handle")],
        calls=[],
    )
    fake.calls = [("/c/caller.ts", f"{PLACEHOLDER_PREFIX}handle", 2, None)]
    stats = resolve_graph(_FakeStore(fake))

    assert stats.edges_resolved_unique == 1
    rewrites = [w for w in fake.writes if w[0] == "rewrite"]
    assert rewrites[0][1]["target"] == "/b/y.ts::handle#2"


# ------------------------------------------------ CRITICAL (round 3): tier
# 2.75 discards its own-namespace narrowing and falls through to the
# FULL un-narrowed candidate list, which then trips ``_pick_by_arity``'s
# multi-namespace refusal guard — turning a completely ordinary same-BC
# method overload into a permanently invisible, unresolved placeholder
# the instant ANY unrelated same-short-named symbol exists in another
# namespace. Reproduced against the real ``_FakeGraph`` harness before
# writing these (see report): today ``edges_left_ambiguous == 1`` and
# ZERO rewrites for the exact repro below; deleting the unrelated BcA
# row makes the same call resolve correctly, proving the unrelated row
# is what breaks it.
#
# USER DECISION ("arity within narrowed set"): in tier 2.75, when
# own-namespace narrowing leaves >1 candidate, run ``_pick_by_arity`` ON
# THAT NARROWED SET and bind at ``confidence="high"`` if it
# disambiguates. Only fall through to the full-list-plus-refusal-guard
# tier when narrowing produced ZERO candidates or ``own_namespace`` is
# ``None``. The refusal guard itself (multi-namespace arity coincidence)
# stays as-is for that fallthrough case — this decision does not touch
# it, and the pre-existing cross-BC mis-bind test
# (test_resolver_prefers_namespace_match_over_cross_bc_arity_coincidence
# above) must stay green unchanged.


def test_resolver_disambiguates_overload_by_arity_within_own_namespace_narrowing() -> (
    None
):
    """The exact CRITICAL repro. BcB's own Factory.Create has TWO
    overloads (arity 1 and 2) in BcB's own namespace — completely
    ordinary code. An unrelated BcA.Factory ALSO defines ``Create``
    (arity 5, different namespace). The caller (BcB.UseCase, namespace
    BcB.Domain) invokes ``Create(x, y)`` — arity 2. Must resolve to
    BcB's own arity-2 overload; the unrelated BcA row must not make it
    vanish into ambiguity."""
    bc_b_factory = "/BcB/Factory.cs"
    bc_b_caller = "/BcB/UseCase.cs"
    bc_a_factory = "/BcA/Factory.cs"
    fake = _FakeGraph(
        defines=[
            (bc_b_factory, "Create", f"{bc_b_factory}::Create#1", 1, "BcB.Domain"),
            (bc_b_factory, "Create", f"{bc_b_factory}::Create#2", 2, "BcB.Domain"),
            (bc_b_caller, "UseCase", f"{bc_b_caller}::UseCase#type", None, "BcB.Domain"),
            (bc_a_factory, "Create", f"{bc_a_factory}::Create#5", 5, "BcA.Domain"),
        ],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}Create", "Create")],
        calls=[],
    )
    fake.calls = [(bc_b_caller, f"{PLACEHOLDER_PREFIX}Create", 2, None)]
    stats = resolve_graph(_FakeStore(fake))

    rewrites = [w for w in fake.writes if w[0] == "rewrite"]
    assert len(rewrites) == 1, (
        f"expected the BcB overload call to resolve, stats={stats}"
    )
    assert rewrites[0][1]["target"] == f"{bc_b_factory}::Create#2"
    assert rewrites[0][1]["conf"] == "high"
    assert stats.edges_left_ambiguous == 0


def test_resolver_same_namespace_overload_arity_collision_still_stays_ambiguous() -> (
    None
):
    """The 'don't overshoot' guard: two overloads in the CALLER's OWN
    namespace that share the SAME arity must still stay ambiguous — the
    fix must disambiguate by arity within the narrowed set, not just
    blindly trust "narrowed to same namespace" as sufficient on its
    own."""
    bc_b_factory = "/BcB/Factory.cs"
    bc_b_caller = "/BcB/UseCase.cs"
    fake = _FakeGraph(
        defines=[
            (bc_b_factory, "Create", f"{bc_b_factory}::Create#int", 1, "BcB.Domain"),
            (bc_b_factory, "Create", f"{bc_b_factory}::Create#str", 1, "BcB.Domain"),
            (bc_b_caller, "UseCase", f"{bc_b_caller}::UseCase#type", None, "BcB.Domain"),
        ],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}Create", "Create")],
        calls=[],
    )
    fake.calls = [(bc_b_caller, f"{PLACEHOLDER_PREFIX}Create", 1, None)]
    stats = resolve_graph(_FakeStore(fake))

    assert stats.edges_left_ambiguous == 1, (
        f"expected the same-arity same-namespace collision to stay "
        f"ambiguous rather than guess, stats={stats}"
    )
    assert not any(w[0] == "rewrite" for w in fake.writes)


# ------------------------------------------------------- MEDIUM: file_namespace
# non-determinism (resolver.py's ``if namespace_str and f_key not in
# file_namespace`` takes the FIRST namespace seen in an UNORDERED
# result_set). A file with two ``namespace`` blocks (valid C#) gets an
# arbitrary one, trusted at confidence="high" — reproduced below by
# supplying the exact SAME two rows for the straddling file in BOTH
# possible orders and observing the resolver silently pick a DIFFERENT
# BC's ``Invoice`` depending purely on row order (verified empirically
# against the real deployed resolver before writing this: order A binds
# BcA's Invoice, order B binds BcB's Invoice — same facts, opposite
# answers).
#
# Recommended fix (this project's call, not user-decided): collect ALL
# non-None namespaces per file into a set rather than "first seen
# wins"; when a file has exactly one distinct namespace, use it; when
# it has 2+ DISTINCT namespaces, treat the file as "namespace unknown"
# (absent from ``file_namespace``) rather than arbitrarily picking one
# — a straddling file is architecturally ambiguous for the "own
# namespace" tier, and guessing wrong at confidence="high" is worse
# than skipping straight to the using-list / ambiguous fallback. This
# also happens to be ORDER-INDEPENDENT by construction (set membership
# doesn't care about insertion order), which is why the two tests below
# assert the SAME (safe, ambiguous) outcome regardless of which order
# the straddling file's two rows arrive in — that in itself is the
# regression guard against a future "just add ORDER BY" fix that
# stabilizes the order but still silently picks one of two valid
# namespaces. A secondary, complementary fix — adding a deterministic
# ``ORDER BY s.start`` to the DEFINES query in ``_GraphState.load`` — is
# also worth doing for defensive-in-depth ordering guarantees on
# ``name_index``'s per-symbol tuples generally, but it is NOT what makes
# these two tests pass; the set-based "2+ distinct -> unknown" collection
# is.


def test_resolver_file_with_two_distinct_namespaces_stays_ambiguous_order_a() -> None:
    bc_a_inv = "/BcA/Invoice.cs"
    bc_b_inv = "/BcB/Invoice.cs"
    weird = "/Weird/Consumer.cs"
    fake = _FakeGraph(
        defines=[
            (bc_a_inv, "Invoice", f"{bc_a_inv}::Invoice#type", None, "NsA"),
            (bc_b_inv, "Invoice", f"{bc_b_inv}::Invoice#type", None, "NsB"),
            (weird, "Thing1", f"{weird}::Thing1#type", None, "NsA"),
            (weird, "Thing2", f"{weird}::Thing2#type", None, "NsB"),
        ],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}Invoice", "Invoice")],
        calls=[],
        reference_edges=[(weird, f"{PLACEHOLDER_PREFIX}Invoice")],
    )
    stats = resolve_graph(_FakeStore(fake))

    assert stats.edges_left_ambiguous == 1, (
        f"a file straddling two distinct namespaces must not let one "
        f"arbitrarily win at confidence=high, stats={stats}"
    )
    assert not any(w[0] == "rewrite_reference" for w in fake.writes)


def test_resolver_file_with_two_distinct_namespaces_stays_ambiguous_order_b() -> None:
    """SAME facts as the test above, DEFINES rows for the straddling file
    supplied in the OPPOSITE order. Must produce the IDENTICAL (safe)
    outcome — proving the fix is order-independent, not just
    order-stabilized."""
    bc_a_inv = "/BcA/Invoice.cs"
    bc_b_inv = "/BcB/Invoice.cs"
    weird = "/Weird/Consumer.cs"
    fake = _FakeGraph(
        defines=[
            (bc_a_inv, "Invoice", f"{bc_a_inv}::Invoice#type", None, "NsA"),
            (bc_b_inv, "Invoice", f"{bc_b_inv}::Invoice#type", None, "NsB"),
            (weird, "Thing2", f"{weird}::Thing2#type", None, "NsB"),
            (weird, "Thing1", f"{weird}::Thing1#type", None, "NsA"),
        ],
        imports=[],
        placeholders=[(f"{PLACEHOLDER_PREFIX}Invoice", "Invoice")],
        calls=[],
        reference_edges=[(weird, f"{PLACEHOLDER_PREFIX}Invoice")],
    )
    stats = resolve_graph(_FakeStore(fake))

    assert stats.edges_left_ambiguous == 1, (
        f"reversing row order must not flip the outcome, stats={stats}"
    )
    assert not any(w[0] == "rewrite_reference" for w in fake.writes)
