"""Post-ingest symbol resolver.

The extractor emits ``CALLS`` edges from each File to a placeholder
``Symbol {key: "name::X"}`` node — there's no way to know *which* X is
meant during a single-file parse. This module runs after ingest, loads
the entire project graph into memory, and re-points each placeholder
edge at a real (defined) Symbol when possible.

Resolution tiers (highest confidence first):

1. **same-file** — F defines X locally → link to F's X.
2. **imported** — F imports a file/module that defines X → link to that.
3. **project-unique** — exactly one File in the project defines X → link
   with medium confidence.
4. **assembly-exposed** — F belongs to a .NET Project whose referenced
   Assemblies expose exactly one Type named X → link to that Type with
   "external" confidence. This is what turns calls like
   ``JsonConvert.SerializeObject(...)`` into resolved edges pointing at
   the Newtonsoft.Json assembly instead of leaving them as orphan
   placeholders.
5. **ambiguous / external** — leave the placeholder in place so the
   structure is preserved but downstream graph queries can filter it.

Imports are resolved best-effort:

- Relative paths (``./bar``, ``../svc/auth``) are probed against project
  files with common extensions (``.ts``, ``.tsx``, ``.js``, ``.jsx``,
  ``.py``, plus ``/index.*``).
- Bare module names (``@acme-ng/security``, ``rxjs``) are treated as
  external — we can't resolve them without a package map.

The resolver is read-mostly; it only writes when something actually
changes. After resolution, placeholder ``name::X`` nodes that lose all
incoming CALLS edges are deleted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..graph.falkor_store import FalkorStore

PLACEHOLDER_PREFIX = "name::"
RESOLVABLE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py")


@dataclass(frozen=True)
class ResolvedEdge:
    file_path: str
    placeholder_key: str  # e.g. "name::getBearerToken"
    target_key: str  # real Symbol or Type key
    confidence: str  # "high" | "medium" | "external"
    target_label: str = "Symbol"  # "Symbol" (in-project) | "Type" (assembly)
    via_assembly: str | None = None  # set when target_label == "Type"
    edge_type: str = "CALLS"  # "CALLS" | "INJECTS"


@dataclass
class ResolverStats:
    placeholders: int = 0
    edges_total: int = 0
    edges_resolved_same_file: int = 0
    edges_resolved_imported: int = 0
    edges_resolved_unique: int = 0
    edges_resolved_assembly: int = 0
    edges_left_ambiguous: int = 0
    edges_left_external: int = 0
    placeholders_deleted: int = 0
    notes: list[str] = field(default_factory=list)


def resolve_graph(graph: FalkorStore) -> ResolverStats:
    """Run the full resolver pass against ``graph``.

    Loads File / Symbol nodes + DEFINES / IMPORTS / CALLS edges into
    memory, computes resolutions, then writes back the rewritten CALLS
    edges and deletes orphaned placeholders.
    """
    state = _GraphState.load(graph)
    stats = ResolverStats(
        placeholders=len(state.placeholders),
        edges_total=len(state.call_edges) + len(state.inject_edges),
    )

    resolutions = _resolve_all(state, stats)
    _apply_resolutions(graph, resolutions)
    deleted = _cleanup_orphans(graph, state, resolutions)
    stats.placeholders_deleted = deleted
    return stats


# ---------------------------------------------------------------- state load


@dataclass
class _GraphState:
    """In-memory snapshot of the parts of the graph the resolver needs."""

    # path -> set of symbol names defined in that file
    file_defines: dict[str, set[str]]
    # path -> list of (module_key, kind) where kind in {"relative", "bare"}
    file_imports: dict[str, list[tuple[str, str]]]
    # placeholder_key -> short name (e.g. "name::foo" -> "foo")
    placeholders: dict[str, str]
    # list of (file_path, placeholder_key, arity) edges to resolve.
    # ``arity`` is -1 when the call site arity is unknown (legacy data).
    call_edges: list[tuple[str, str, int]]
    # symbol_name -> list of (file_path, full_symbol_key, param_count) defining it.
    # ``param_count`` is ``None`` for non-callable kinds; resolver
    # ignores arity matching when either side is missing.
    name_index: dict[str, list[tuple[str, str, int | None]]]
    # set of project file paths (resolved absolute), for relative import lookup
    project_files: set[str]
    # file_path -> project_key (CONTAINED_IN); empty for non-.NET projects
    file_project: dict[str, str]
    # project_key -> set of assembly_key references (USES_ASSEMBLY)
    project_assemblies: dict[str, set[str]]
    # type_name -> list of (assembly_key, type_key) tuples
    type_index: dict[str, list[tuple[str, str]]]
    # list of (file_path, placeholder_key) INJECTS edges to resolve
    # alongside CALLS — same resolution rules, different edge type.
    inject_edges: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, graph: FalkorStore) -> _GraphState:
        rows = graph.graph.query(
            "MATCH (f:File)-[:DEFINES]->(s:Symbol) "
            "WHERE s.unresolved IS NULL "
            "RETURN f.key, s.name, s.key, s.params"
        ).result_set
        file_defines: dict[str, set[str]] = defaultdict(set)
        name_index: dict[str, list[tuple[str, str, int | None]]] = defaultdict(list)
        for row in rows:
            f_key, s_name, s_key = row[0], row[1], row[2]
            params = row[3] if len(row) > 3 else None
            params_int = int(params) if isinstance(params, (int, float)) else None
            file_defines[f_key].add(s_name)
            name_index[s_name].append((f_key, s_key, params_int))

        rows = graph.graph.query(
            "MATCH (f:File)-[:IMPORTS]->(m:Module) RETURN f.key, m.key"
        ).result_set
        file_imports: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for f_key, m_key in rows:
            kind = "relative" if (m_key.startswith(".") or m_key.startswith("/")) else "bare"
            file_imports[f_key].append((m_key, kind))

        rows = graph.graph.query(
            "MATCH (s:Symbol) WHERE s.key STARTS WITH $p RETURN s.key, s.name",
            {"p": PLACEHOLDER_PREFIX},
        ).result_set
        placeholders: dict[str, str] = {}
        for s_key, s_name in rows:
            # fall back to stripping prefix if the name prop is missing
            placeholders[s_key] = s_name or s_key[len(PLACEHOLDER_PREFIX) :]

        rows = graph.graph.query(
            "MATCH (f:File)-[r:CALLS]->(s:Symbol) "
            "WHERE s.key STARTS WITH $p "
            "RETURN f.key, s.key, r.args",
            {"p": PLACEHOLDER_PREFIX},
        ).result_set
        call_edges: list[tuple[str, str, int]] = []
        for row in rows:
            f = row[0]
            s = row[1]
            arity_raw = row[2] if len(row) > 2 else None
            arity = int(arity_raw) if isinstance(arity_raw, (int, float)) else -1
            call_edges.append((f, s, arity))

        rows = graph.graph.query(
            "MATCH (f:File)-[:INJECTS]->(s:Symbol) "
            "WHERE s.key STARTS WITH $p "
            "RETURN f.key, s.key",
            {"p": PLACEHOLDER_PREFIX},
        ).result_set
        inject_edges: list[tuple[str, str]] = [(f, s) for f, s in rows]

        # File→Project containment (only emitted for .NET files).
        rows = graph.graph.query(
            "MATCH (f:File)-[:CONTAINED_IN]->(p:Project) RETURN f.key, p.key"
        ).result_set
        file_project: dict[str, str] = {f: p for f, p in rows}

        # Project→Assembly use edges.
        rows = graph.graph.query(
            "MATCH (p:Project)-[:USES_ASSEMBLY]->(a:Assembly) "
            "RETURN p.key, a.key"
        ).result_set
        project_assemblies: dict[str, set[str]] = defaultdict(set)
        for p_key, a_key in rows:
            project_assemblies[p_key].add(a_key)

        # Type name index across all indexed assemblies.
        rows = graph.graph.query(
            "MATCH (a:Assembly)-[:EXPOSES_TYPE]->(t:Type) "
            "RETURN t.name, t.key, a.key"
        ).result_set
        type_index: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for t_name, t_key, a_key in rows:
            type_index[t_name].append((a_key, t_key))

        return cls(
            file_defines=dict(file_defines),
            file_imports=dict(file_imports),
            placeholders=placeholders,
            call_edges=call_edges,
            name_index=dict(name_index),
            project_files=set(file_defines.keys()),
            file_project=file_project,
            project_assemblies=dict(project_assemblies),
            type_index=dict(type_index),
            inject_edges=inject_edges,
        )


# ---------------------------------------------------------------- resolution


def _resolve_all(state: _GraphState, stats: ResolverStats) -> list[ResolvedEdge]:
    resolutions: list[ResolvedEdge] = []
    # per-file cache: imported file path -> symbols defined there
    import_cache: dict[str, dict[str, list[tuple[str, str, int | None]]]] = {}

    # Normalise call_edges to (file, placeholder, arity); inject_edges
    # don't carry call-site arity (DI is by type, not by call arity).
    norm_calls = [(f, p, a) for f, p, a in state.call_edges]
    norm_injects = [(f, p, -1) for f, p in state.inject_edges]
    edge_specs: list[tuple[str, list[tuple[str, str, int]]]] = [
        ("CALLS", norm_calls),
        ("INJECTS", norm_injects),
    ]

    for edge_type, edges in edge_specs:
        for file_path, placeholder_key, arity in edges:
            name = state.placeholders.get(placeholder_key)
            if not name:
                stats.edges_left_external += 1
                continue

            # (1) same-file
            if name in state.file_defines.get(file_path, ()):
                target = _pick_target(
                    state.name_index[name],
                    preferred_file=file_path,
                    arity=arity,
                )
                if target is not None:
                    resolutions.append(
                        ResolvedEdge(
                            file_path,
                            placeholder_key,
                            target,
                            "high",
                            edge_type=edge_type,
                        )
                    )
                    stats.edges_resolved_same_file += 1
                    continue

            # (2) imported
            if file_path not in import_cache:
                import_cache[file_path] = _imported_symbols(state, file_path)
            imported = import_cache[file_path].get(name, [])
            if len(imported) == 1:
                resolutions.append(
                    ResolvedEdge(
                        file_path,
                        placeholder_key,
                        imported[0][1],
                        "high",
                        edge_type=edge_type,
                    )
                )
                stats.edges_resolved_imported += 1
                continue
            if len(imported) > 1:
                # Try arity-based disambiguation across imported candidates.
                arity_match = _pick_by_arity(imported, arity)
                if arity_match is not None:
                    resolutions.append(
                        ResolvedEdge(
                            file_path,
                            placeholder_key,
                            arity_match,
                            "high",
                            edge_type=edge_type,
                        )
                    )
                    stats.edges_resolved_imported += 1
                    continue
                stats.edges_left_ambiguous += 1
                continue

            # (3) project-unique (with arity tiebreak)
            candidates = state.name_index.get(name, [])
            if len(candidates) == 1:
                resolutions.append(
                    ResolvedEdge(
                        file_path,
                        placeholder_key,
                        candidates[0][1],
                        "medium",
                        edge_type=edge_type,
                    )
                )
                stats.edges_resolved_unique += 1
                continue
            if len(candidates) > 1:
                arity_match = _pick_by_arity(candidates, arity)
                if arity_match is not None:
                    resolutions.append(
                        ResolvedEdge(
                            file_path,
                            placeholder_key,
                            arity_match,
                            "medium",
                            edge_type=edge_type,
                        )
                    )
                    stats.edges_resolved_unique += 1
                    continue
                stats.edges_left_ambiguous += 1
                continue

            # (4) assembly-exposed — only for .NET files whose project
            # we indexed. Match the name against Type nodes from any
            # assembly the file's project references; require a unique
            # hit so we never coin-flip across overlapping surface
            # (``Path`` in BCL and a 3rd-party lib). Same rules for
            # CALLS and INJECTS — a Razor file injecting
            # ``IUserService`` resolves through this path too.
            asm_target = _resolve_via_assembly(file_path, name, state)
            if asm_target is not None:
                target_key, asm_key = asm_target
                resolutions.append(
                    ResolvedEdge(
                        file_path=file_path,
                        placeholder_key=placeholder_key,
                        target_key=target_key,
                        confidence="external",
                        target_label="Type",
                        via_assembly=asm_key,
                        edge_type=edge_type,
                    )
                )
                stats.edges_resolved_assembly += 1
                continue

            stats.edges_left_external += 1

    return resolutions


def _resolve_via_assembly(
    file_path: str, name: str, state: _GraphState
) -> tuple[str, str] | None:
    """Pick the unique ``(type_key, assembly_key)`` that resolves ``name``.

    Returns ``None`` when:
    * the file isn't contained in any project (non-.NET, or owned by a
      project we didn't index),
    * the type name isn't exposed by any indexed assembly,
    * multiple referenced assemblies expose the same type name
      (would be a coin flip; safer to leave it unresolved so the agent
      sees ambiguity).
    """
    proj_key = state.file_project.get(file_path)
    if proj_key is None:
        return None
    asm_set = state.project_assemblies.get(proj_key)
    if not asm_set:
        return None
    candidates = state.type_index.get(name, [])
    matches = [(t_key, a_key) for a_key, t_key in candidates if a_key in asm_set]
    if len(matches) != 1:
        return None
    type_key, asm_key = matches[0]
    return type_key, asm_key


def _pick_target(
    candidates: list[tuple[str, str, int | None]],
    *,
    preferred_file: str | None,
    arity: int = -1,
) -> str | None:
    """Pick the best symbol key from same-name candidates.

    Prefers (in order):
    1. Definitions in ``preferred_file`` (same-file match).
    2. The first remaining candidate.

    When ``arity`` is supplied (>= 0) and matches a candidate's
    ``params``, that candidate wins the same-file tier too — handy
    when a file declares two overloads of the same method.
    """
    if not candidates:
        return None
    if preferred_file is not None:
        same_file = [c for c in candidates if c[0] == preferred_file]
        if same_file:
            if arity >= 0:
                for f, k, p in same_file:
                    if p == arity:
                        return k
            return same_file[0][1]
    return candidates[0][1]


def _pick_by_arity(
    candidates: list[tuple[str, str, int | None]], arity: int
) -> str | None:
    """Return the unique candidate whose param count matches ``arity``.

    Returns ``None`` when arity is unknown (call_edge arity == -1) or
    when the match isn't unique. The resolver treats any of those as
    "ambiguous" — we never coin-flip across overloads.
    """
    if arity < 0:
        return None
    matches = [c for c in candidates if c[2] == arity]
    if len(matches) == 1:
        return matches[0][1]
    return None


def _imported_symbols(
    state: _GraphState, file_path: str
) -> dict[str, list[tuple[str, str, int | None]]]:
    """Return {symbol_name -> [(defining_file, symbol_key, params)]} reachable from ``file_path``.

    Only relative imports are followed (bare module specifiers like
    ``@scope/lib`` are treated as external).
    """
    out: dict[str, list[tuple[str, str, int | None]]] = defaultdict(list)
    file_dir = Path(file_path).parent
    for mod_key, kind in state.file_imports.get(file_path, []):
        if kind != "relative":
            continue
        target_file = _resolve_relative_import(file_dir, mod_key, state.project_files)
        if target_file is None:
            continue
        for sym_name in state.file_defines.get(target_file, ()):
            for f, k, params in state.name_index.get(sym_name, []):
                if f == target_file:
                    out[sym_name].append((f, k, params))
    return out


def _resolve_relative_import(
    file_dir: Path, mod_key: str, project_files: set[str]
) -> str | None:
    """Resolve a relative import specifier to an actual project file path.

    Probes common TS/JS/Python extensions and ``/index.*`` variants.
    Returns ``None`` if no candidate matches a known project file.
    """
    base = (file_dir / mod_key).resolve()
    base_str = str(base)

    # exact match (caller wrote ``./bar.ts``)
    if base_str in project_files:
        return base_str

    for suf in RESOLVABLE_SUFFIXES:
        cand = base_str + suf
        if cand in project_files:
            return cand

    # directory index
    for suf in RESOLVABLE_SUFFIXES:
        cand = str(base / f"index{suf}")
        if cand in project_files:
            return cand

    return None


# ---------------------------------------------------------------- writeback


def _apply_resolutions(
    graph: FalkorStore, resolutions: list[ResolvedEdge]
) -> None:
    """Rewrite resolved edges from placeholder to real targets.

    Four batches: cross-product of (edge_type, target_label).
    FalkorDB's MATCH has no polymorphism, so each combination needs
    its own Cypher pattern.
    """
    if not resolutions:
        return

    def _bucket(edge_type: str, label: str) -> list[dict[str, object]]:
        return [
            {
                "file": r.file_path,
                "placeholder": r.placeholder_key,
                "target": r.target_key,
                "conf": r.confidence,
                "via": r.via_assembly,
            }
            for r in resolutions
            if r.edge_type == edge_type and r.target_label == label
        ]

    queries: list[tuple[str, list[dict[str, object]]]] = [
        (
            """
            UNWIND $rows AS row
            MATCH (f:File {key: row.file})-[old:CALLS]->(:Symbol {key: row.placeholder})
            MATCH (t:Symbol {key: row.target})
            DELETE old
            MERGE (f)-[r:CALLS]->(t)
            SET r.confidence = row.conf, r.resolved = true
            """,
            _bucket("CALLS", "Symbol"),
        ),
        (
            """
            UNWIND $rows AS row
            MATCH (f:File {key: row.file})-[old:CALLS]->(:Symbol {key: row.placeholder})
            MATCH (t:Type {key: row.target})
            DELETE old
            MERGE (f)-[r:CALLS]->(t)
            SET r.confidence = row.conf,
                r.resolved = true,
                r.via_assembly = row.via
            """,
            _bucket("CALLS", "Type"),
        ),
        (
            """
            UNWIND $rows AS row
            MATCH (f:File {key: row.file})-[old:INJECTS]->(:Symbol {key: row.placeholder})
            MATCH (t:Symbol {key: row.target})
            DELETE old
            MERGE (f)-[r:INJECTS]->(t)
            SET r.confidence = row.conf, r.resolved = true
            """,
            _bucket("INJECTS", "Symbol"),
        ),
        (
            """
            UNWIND $rows AS row
            MATCH (f:File {key: row.file})-[old:INJECTS]->(:Symbol {key: row.placeholder})
            MATCH (t:Type {key: row.target})
            DELETE old
            MERGE (f)-[r:INJECTS]->(t)
            SET r.confidence = row.conf,
                r.resolved = true,
                r.via_assembly = row.via
            """,
            _bucket("INJECTS", "Type"),
        ),
    ]
    for query, rows in queries:
        if rows:
            graph.graph.query(query, {"rows": rows})


def _cleanup_orphans(
    graph: FalkorStore,
    state: _GraphState,
    resolutions: list[ResolvedEdge],
) -> int:
    """Delete placeholder nodes whose CALLS *and* INJECTS edges are gone.

    A placeholder is orphan only when nothing points at it via either
    relation — a Razor file injecting an unresolved interface keeps
    its placeholder alive even when no source calls it.
    """
    if not state.placeholders:
        return 0

    res = graph.graph.query(
        """
        MATCH (s:Symbol)
        WHERE s.key STARTS WITH $p
          AND NOT ( ()-[:CALLS]->(s) )
          AND NOT ( ()-[:INJECTS]->(s) )
        WITH s, count(s) AS c
        DELETE s
        RETURN c
        """,
        {"p": PLACEHOLDER_PREFIX},
    )
    # FalkorDB returns nodes_deleted in result statistics; fall back to a
    # second count query if not available.
    deleted = getattr(res, "nodes_deleted", None)
    if deleted is None:
        # best-effort estimate from local state
        rewritten = {r.placeholder_key for r in resolutions}
        deleted = sum(
            1 for k in state.placeholders if k in rewritten
        )
    return int(deleted)
