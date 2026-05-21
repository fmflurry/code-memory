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
4. **ambiguous / external** — leave the placeholder in place so the
   structure is preserved but downstream graph queries can filter it.

Imports are resolved best-effort:

- Relative paths (``./bar``, ``../svc/auth``) are probed against project
  files with common extensions (``.ts``, ``.tsx``, ``.js``, ``.jsx``,
  ``.py``, plus ``/index.*``).
- Bare module names (``@internal-ng/security``, ``rxjs``) are treated as
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
    target_key: str  # real Symbol key, e.g. "/abs/path::getBearerToken#12"
    confidence: str  # "high" | "medium"


@dataclass
class ResolverStats:
    placeholders: int = 0
    edges_total: int = 0
    edges_resolved_same_file: int = 0
    edges_resolved_imported: int = 0
    edges_resolved_unique: int = 0
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
        edges_total=len(state.call_edges),
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
    # list of (file_path, placeholder_key) edges to resolve
    call_edges: list[tuple[str, str]]
    # symbol_name -> list of (file_path, full_symbol_key) defining it
    name_index: dict[str, list[tuple[str, str]]]
    # set of project file paths (resolved absolute), for relative import lookup
    project_files: set[str]

    @classmethod
    def load(cls, graph: FalkorStore) -> _GraphState:
        rows = graph.graph.query(
            "MATCH (f:File)-[:DEFINES]->(s:Symbol) "
            "WHERE s.unresolved IS NULL "
            "RETURN f.key, s.name, s.key"
        ).result_set
        file_defines: dict[str, set[str]] = defaultdict(set)
        name_index: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for f_key, s_name, s_key in rows:
            file_defines[f_key].add(s_name)
            name_index[s_name].append((f_key, s_key))

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
            "MATCH (f:File)-[:CALLS]->(s:Symbol) "
            "WHERE s.key STARTS WITH $p "
            "RETURN f.key, s.key",
            {"p": PLACEHOLDER_PREFIX},
        ).result_set
        call_edges: list[tuple[str, str]] = [(f, s) for f, s in rows]

        return cls(
            file_defines=dict(file_defines),
            file_imports=dict(file_imports),
            placeholders=placeholders,
            call_edges=call_edges,
            name_index=dict(name_index),
            project_files=set(file_defines.keys()),
        )


# ---------------------------------------------------------------- resolution


def _resolve_all(state: _GraphState, stats: ResolverStats) -> list[ResolvedEdge]:
    resolutions: list[ResolvedEdge] = []
    # per-file cache: imported file path -> symbols defined there
    import_cache: dict[str, dict[str, list[tuple[str, str]]]] = {}

    for file_path, placeholder_key in state.call_edges:
        name = state.placeholders.get(placeholder_key)
        if not name:
            stats.edges_left_external += 1
            continue

        # (1) same-file
        if name in state.file_defines.get(file_path, ()):
            target = _pick_target(state.name_index[name], preferred_file=file_path)
            if target is not None:
                resolutions.append(
                    ResolvedEdge(file_path, placeholder_key, target, "high")
                )
                stats.edges_resolved_same_file += 1
                continue

        # (2) imported
        if file_path not in import_cache:
            import_cache[file_path] = _imported_symbols(state, file_path)
        imported = import_cache[file_path].get(name, [])
        if len(imported) == 1:
            resolutions.append(
                ResolvedEdge(file_path, placeholder_key, imported[0][1], "high")
            )
            stats.edges_resolved_imported += 1
            continue
        if len(imported) > 1:
            stats.edges_left_ambiguous += 1
            continue

        # (3) project-unique
        candidates = state.name_index.get(name, [])
        if len(candidates) == 1:
            resolutions.append(
                ResolvedEdge(file_path, placeholder_key, candidates[0][1], "medium")
            )
            stats.edges_resolved_unique += 1
            continue
        if len(candidates) > 1:
            stats.edges_left_ambiguous += 1
        else:
            stats.edges_left_external += 1

    return resolutions


def _pick_target(
    candidates: list[tuple[str, str]], *, preferred_file: str | None
) -> str | None:
    if not candidates:
        return None
    if preferred_file is not None:
        for f, k in candidates:
            if f == preferred_file:
                return k
    return candidates[0][1]


def _imported_symbols(
    state: _GraphState, file_path: str
) -> dict[str, list[tuple[str, str]]]:
    """Return {symbol_name -> [(defining_file, symbol_key)]} reachable from ``file_path``.

    Only relative imports are followed (bare module specifiers like
    ``@scope/lib`` are treated as external).
    """
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    file_dir = Path(file_path).parent
    for mod_key, kind in state.file_imports.get(file_path, []):
        if kind != "relative":
            continue
        target_file = _resolve_relative_import(file_dir, mod_key, state.project_files)
        if target_file is None:
            continue
        for sym_name in state.file_defines.get(target_file, ()):
            for f, k in state.name_index.get(sym_name, []):
                if f == target_file:
                    out[sym_name].append((f, k))
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
    """Rewrite resolved CALLS edges from placeholder to real Symbol nodes."""
    if not resolutions:
        return
    payload = [
        {
            "file": r.file_path,
            "placeholder": r.placeholder_key,
            "target": r.target_key,
            "conf": r.confidence,
        }
        for r in resolutions
    ]
    graph.graph.query(
        """
        UNWIND $rows AS row
        MATCH (f:File {key: row.file})-[old:CALLS]->(:Symbol {key: row.placeholder})
        MATCH (t:Symbol {key: row.target})
        DELETE old
        MERGE (f)-[r:CALLS]->(t)
        SET r.confidence = row.conf, r.resolved = true
        """,
        {"rows": payload},
    )


def _cleanup_orphans(
    graph: FalkorStore,
    state: _GraphState,
    resolutions: list[ResolvedEdge],
) -> int:
    """Delete placeholder nodes whose CALLS edges were all rewritten."""
    if not state.placeholders:
        return 0

    # Quick check via Cypher: drop any name::X Symbol with no incoming edges.
    res = graph.graph.query(
        """
        MATCH (s:Symbol)
        WHERE s.key STARTS WITH $p
          AND NOT ( ()-[:CALLS]->(s) )
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
