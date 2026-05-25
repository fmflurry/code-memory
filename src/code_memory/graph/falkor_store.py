from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from falkordb import FalkorDB

from ..config import CONFIG

NodeLabel = str  # File | Symbol | Module
EdgeType = str  # IMPORTS | CALLS | DEFINES | EXPORTS


@dataclass
class GraphNode:
    label: NodeLabel
    key: str  # stable identity, e.g. absolute path or fqn
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    type: EdgeType
    src_label: NodeLabel
    src_key: str
    dst_label: NodeLabel
    dst_key: str
    props: dict[str, Any] = field(default_factory=dict)


class FalkorStore:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        graph_name: str | None = None,
    ) -> None:
        self.db = FalkorDB(
            host=host or CONFIG.falkor_host,
            port=port or CONFIG.falkor_port,
        )
        self.graph = self.db.select_graph(graph_name or CONFIG.falkor_graph)
        # Two server-side tunables that bite on large graphs (private-monorepo,
        # 200K symbols / 270K calls):
        #
        # 1. RESULTSET_SIZE — default 10000. Silently truncates the
        #    resolver's full-graph snapshot, which made "all calls
        #    resolved as external" the visible symptom because half the
        #    placeholders never made it into the in-memory index.
        # 2. TIMEOUT_DEFAULT — default 1000ms. The first run of a
        #    topology query (callers/callees) routinely takes 2-5 s
        #    while Falkor compiles + warms its planner; cached runs are
        #    sub-100ms. The 1s cap killed every cold call.
        for cmd in (
            ("GRAPH.CONFIG", "SET", "RESULTSET_SIZE", "-1"),
            ("GRAPH.CONFIG", "SET", "TIMEOUT_DEFAULT", "30000"),
            ("GRAPH.CONFIG", "SET", "TIMEOUT_MAX", "60000"),
        ):
            try:
                self.db.connection.execute_command(*cmd)
            except Exception:  # noqa: BLE001 — best-effort, server defaults persist
                pass

    def ensure_indexes(self) -> None:
        for label in ("File", "Symbol", "Module"):
            try:
                self.graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.key)")
            except Exception:
                # index may already exist
                pass

    def upsert_nodes(
        self,
        nodes: Iterable[GraphNode],
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
        """Upsert nodes and stamp their temporal lifecycle when ``head_sha``
        is provided.

        Stamping rules (per ``CHANGELOG`` "Temporal model"):

        - On first insert: ``first_seen_sha = last_seen_sha = head_sha``,
          ditto for the matching ``_ord`` integers when ``head_ord`` is
          supplied (enables range comparisons across SHAs).
        - On subsequent ingest of the same key: ``last_seen_sha = head_sha``,
          ``first_seen_sha`` preserved (COALESCE handles legacy rows that
          have no value yet).
        - ``invalid_sha`` / ``invalid_ord`` / ``invalid_at`` are always
          cleared on a successful upsert — the node is alive again at
          this SHA.
        """
        for n in nodes:
            if head_sha is None:
                self.graph.query(
                    f"MERGE (n:{n.label} {{key: $key}}) SET n += $props",
                    {"key": n.key, "props": n.props},
                )
                continue
            self.graph.query(
                f"""
                MERGE (n:{n.label} {{key: $key}})
                ON CREATE SET n.first_seen_sha = $head,
                              n.last_seen_sha = $head,
                              n.first_seen_ord = $ord,
                              n.last_seen_ord = $ord
                ON MATCH  SET n.first_seen_sha = COALESCE(n.first_seen_sha, $head),
                              n.last_seen_sha = $head,
                              n.first_seen_ord = COALESCE(n.first_seen_ord, $ord),
                              n.last_seen_ord = $ord
                SET n += $props
                SET n.invalid_sha = NULL,
                    n.invalid_ord = NULL,
                    n.invalid_at = NULL
                """,
                {
                    "key": n.key,
                    "props": n.props,
                    "head": head_sha,
                    "ord": head_ord,
                },
            )

    def upsert_edges(
        self,
        edges: Iterable[GraphEdge],
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
        """Upsert edges with the same temporal stamping as :meth:`upsert_nodes`."""
        for e in edges:
            if head_sha is None:
                self.graph.query(
                    f"""
                    MERGE (a:{e.src_label} {{key: $src}})
                    MERGE (b:{e.dst_label} {{key: $dst}})
                    MERGE (a)-[r:{e.type}]->(b)
                    SET r += $props
                    """,
                    {"src": e.src_key, "dst": e.dst_key, "props": e.props},
                )
                continue
            self.graph.query(
                f"""
                MERGE (a:{e.src_label} {{key: $src}})
                MERGE (b:{e.dst_label} {{key: $dst}})
                MERGE (a)-[r:{e.type}]->(b)
                ON CREATE SET r.first_seen_sha = $head,
                              r.last_seen_sha = $head,
                              r.first_seen_ord = $ord,
                              r.last_seen_ord = $ord
                ON MATCH  SET r.first_seen_sha = COALESCE(r.first_seen_sha, $head),
                              r.last_seen_sha = $head,
                              r.first_seen_ord = COALESCE(r.first_seen_ord, $ord),
                              r.last_seen_ord = $ord
                SET r += $props
                SET r.invalid_sha = NULL,
                    r.invalid_ord = NULL,
                    r.invalid_at = NULL
                """,
                {
                    "src": e.src_key,
                    "dst": e.dst_key,
                    "props": e.props,
                    "head": head_sha,
                    "ord": head_ord,
                },
            )

    def neighbors(
        self,
        label: NodeLabel,
        key: str,
        depth: int = 1,
        edge_types: tuple[EdgeType, ...] | None = None,
    ) -> list[dict[str, Any]]:
        rel = f":{'|'.join(edge_types)}" if edge_types else ""
        q = (
            f"MATCH (n:{label} {{key: $key}})-[{rel}*1..{depth}]-(m) "
            "RETURN DISTINCT labels(m) AS labels, m.key AS key, m AS node"
        )
        result = self.graph.query(q, {"key": key})
        out: list[dict[str, Any]] = []
        for row in result.result_set:
            labels, k, node = row
            out.append({"labels": labels, "key": k, "props": dict(node.properties)})
        return out

    def delete_file(
        self,
        path: str,
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
        """Remove or tombstone a File node and its owned graph elements.

        When ``head_sha`` is provided, the File, its DEFINES-linked
        Symbols (excluding shared ``name::X`` placeholders), and all
        edges touching the File are marked with
        ``invalid_sha`` / ``invalid_ord`` / ``invalid_at`` instead of
        being deleted. The triple lets vacuum filter by exact SHA,
        ordinal range, or wall-clock age without re-resolving git on
        every query.

        When ``head_sha`` is ``None`` (non-git ingest, legacy callers),
        the old hard-delete behaviour is kept.
        """
        if head_sha is None:
            self.graph.query(
                "MATCH (f:File {key: $key}) DETACH DELETE f",
                {"key": path},
            )
            return
        now_ts = time.time()
        params = {
            "key": path,
            "head": head_sha,
            "ord": head_ord,
            "ts": now_ts,
        }
        self.graph.query(
            """
            MATCH (f:File {key: $key})
            SET f.invalid_sha = $head,
                f.invalid_ord = $ord,
                f.invalid_at = $ts
            """,
            params,
        )
        self.graph.query(
            """
            MATCH (f:File {key: $key})-[:DEFINES]->(s:Symbol)
            WHERE s.unresolved IS NULL
            SET s.invalid_sha = $head,
                s.invalid_ord = $ord,
                s.invalid_at = $ts
            """,
            params,
        )
        self.graph.query(
            """
            MATCH (f:File {key: $key})-[r]-()
            SET r.invalid_sha = $head,
                r.invalid_ord = $ord,
                r.invalid_at = $ts
            """,
            params,
        )

    # ------------------------------------------------------------------
    # Vacuum + time-travel — bound monotonic graph growth and let callers
    # query the historic state of the codebase at any past SHA.

    def vacuum(
        self,
        *,
        before_ord: int | None = None,
        older_than_seconds: float | None = None,
        drop_all: bool = False,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Drop tombstoned nodes/edges according to the supplied policy.

        Exactly one of ``before_ord`` / ``older_than_seconds`` / ``drop_all``
        must be set. ``dry_run`` reports counts without writing.

        Returns ``{"files": N, "symbols": N, "edges": N}`` of items
        affected (counted before deletion when ``dry_run`` is True).
        """
        modes = sum(
            x is not None and x is not False
            for x in (before_ord, older_than_seconds, drop_all or None)
        )
        if modes != 1:
            raise ValueError(
                "vacuum requires exactly one of before_ord / "
                "older_than_seconds / drop_all"
            )
        # Build a predicate matching the chosen policy. Wrapped in
        # ``invalid_sha IS NOT NULL`` so live nodes are never touched.
        if drop_all:
            pred = "n.invalid_sha IS NOT NULL"
            params: dict[str, Any] = {}
        elif before_ord is not None:
            pred = (
                "n.invalid_sha IS NOT NULL "
                "AND n.invalid_ord IS NOT NULL "
                "AND n.invalid_ord <= $ord"
            )
            params = {"ord": before_ord}
        else:
            assert older_than_seconds is not None  # narrowing for mypy
            cutoff = time.time() - older_than_seconds
            pred = (
                "n.invalid_sha IS NOT NULL "
                "AND n.invalid_at IS NOT NULL "
                "AND n.invalid_at <= $cutoff"
            )
            params = {"cutoff": cutoff}

        file_count = self.graph.query(
            f"MATCH (n:File) WHERE {pred} RETURN count(n)",
            params,
        ).result_set[0][0]
        sym_count = self.graph.query(
            f"MATCH (n:Symbol) WHERE {pred} RETURN count(n)",
            params,
        ).result_set[0][0]
        # Falkor edge counting: alias the relationship as the predicate
        # target so the same ``n.invalid_…`` predicate works.
        edge_pred = pred.replace("n.", "r.")
        edge_count = self.graph.query(
            f"MATCH ()-[r]-() WHERE {edge_pred} RETURN count(r)",
            params,
        ).result_set[0][0]

        out = {
            "files": int(file_count),
            "symbols": int(sym_count),
            "edges": int(edge_count) // 2,  # undirected double-count fix
        }
        if dry_run:
            return out

        self.graph.query(
            f"MATCH (n:File) WHERE {pred} DETACH DELETE n",
            params,
        )
        self.graph.query(
            f"MATCH (n:Symbol) WHERE {pred} DETACH DELETE n",
            params,
        )
        # Dangling tombstoned edges between live nodes (rare — usually a
        # node is deleted alongside the edge — but possible if only one
        # endpoint got tombstoned). DELETE r leaves the endpoints alone.
        self.graph.query(
            f"MATCH ()-[r]-() WHERE {edge_pred} DELETE r",
            params,
        )
        return out

    def at_sha(
        self,
        sha: str,
        sha_ord: int,
        *,
        label: NodeLabel = "Symbol",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return ``label`` nodes that were alive at the supplied SHA.

        "Alive at SHA X" means ``first_seen_ord <= X_ord`` AND
        (``invalid_ord IS NULL`` OR ``invalid_ord > X_ord``). Requires the
        nodes to carry topological ordinals — anything ingested before
        the temporal upgrade returns nothing here even if it existed at
        that SHA, because we can't compare its lifecycle without an
        ordinal.

        Pass both ``sha`` and ``sha_ord`` so callers can resolve the
        ordinal once per query (``git_delta.commit_ordinal``).
        """
        rows = self.graph.query(
            f"""
            MATCH (n:{label})
            WHERE n.first_seen_ord IS NOT NULL
              AND n.first_seen_ord <= $ord
              AND (n.invalid_ord IS NULL OR n.invalid_ord > $ord)
            RETURN n.key, n.first_seen_sha, n.last_seen_sha, n.invalid_sha
            LIMIT $limit
            """,
            {"ord": sha_ord, "limit": limit},
        ).result_set
        return [
            {
                "key": key,
                "first_seen_sha": fs,
                "last_seen_sha": ls,
                "invalid_sha": iv,
                "at_sha": sha,
            }
            for key, fs, ls, iv in rows
        ]

    def callers_at_sha(
        self,
        symbol_name: str,
        sha: str,
        sha_ord: int,
    ) -> list[dict[str, Any]]:
        """``callers(symbol_name)`` but as the graph looked at ``sha``.

        Tombstones whose ``invalid_ord > sha_ord`` count as alive, so
        questions like "what called X *before* commit Y deleted it" stop
        needing a worktree checkout.
        """
        rows = self.graph.query(
            """
            MATCH (s:Symbol {name: $name})
            WHERE s.unresolved IS NULL
              AND s.first_seen_ord IS NOT NULL
              AND s.first_seen_ord <= $ord
              AND (s.invalid_ord IS NULL OR s.invalid_ord > $ord)
            MATCH (caller:File)-[c:CALLS|REFERENCES]->(s)
            WHERE caller.first_seen_ord <= $ord
              AND (caller.invalid_ord IS NULL OR caller.invalid_ord > $ord)
              AND c.first_seen_ord <= $ord
              AND (c.invalid_ord IS NULL OR c.invalid_ord > $ord)
            RETURN DISTINCT caller.key, s.file, s.start, s.end, s.kind
            """,
            {"name": symbol_name, "ord": sha_ord},
        ).result_set
        return [
            {
                "caller": caller_key,
                "target_file": file_key,
                "target_start": start,
                "target_end": end,
                "target_kind": kind,
                "at_sha": sha,
            }
            for caller_key, file_key, start, end, kind in rows
        ]

    def drift(self, head_sha: str) -> list[dict[str, Any]]:
        """Return symbols whose ``last_seen_sha`` doesn't match ``head_sha``.

        Two categories surface:

        - **Stale**: ``invalid_sha`` is set (the symbol was tombstoned).
        - **Drifted**: ``invalid_sha`` is NULL but the last ingest that
          saw the node was an older HEAD — usually a hint that an
          incremental ingest missed the file or it moved.
        """
        rows = self.graph.query(
            """
            MATCH (s:Symbol)
            WHERE s.unresolved IS NULL
              AND (s.invalid_sha IS NOT NULL OR s.last_seen_sha <> $head)
            RETURN s.key, s.name, s.file, s.last_seen_sha, s.invalid_sha
            """,
            {"head": head_sha},
        ).result_set
        out: list[dict[str, Any]] = []
        for key, name, file_path, last_seen, invalid in rows:
            out.append(
                {
                    "key": key,
                    "name": name,
                    "file": file_path,
                    "last_seen_sha": last_seen,
                    "invalid_sha": invalid,
                    "status": "tombstoned" if invalid else "drifted",
                }
            )
        return out

    def clear_graph(self) -> None:
        """Remove every node + edge in this project's graph."""
        self.graph.query("MATCH (n) DETACH DELETE n")

    # ------------------------------------------------------------------
    # Topology queries — exposed via MCP/CLI as `codememory_<op>` tools.
    # All return ``list[dict]`` so callers can render or JSON-serialize.
    # ``depth`` is capped at 3 to keep traversal bounded.

    def callers(self, symbol_name: str, depth: int = 1) -> list[dict[str, Any]]:
        """Files (and their symbols) that call or reference ``symbol_name``.

        Unions ``CALLS`` and ``REFERENCES`` edges so an interface like
        ``IFooService`` surfaces both the call sites of its members *and*
        the files that declare a parameter / field / base list of that
        type. Returns one row per direct caller file; symbol coordinates
        of the called definition are included so the user can jump to it.
        """
        depth = max(1, min(depth, 3))
        # Tombstoned symbols / files filtered out by default so callers
        # see the live HEAD view. Pre-temporal rows have NULL invalid_sha
        # so the predicate is a no-op for legacy data. ``LIMIT`` keeps hub
        # symbols (e.g. C# base classes with thousands of callers) from
        # timing out the Falkor query — agents that need more should page.
        q = (
            "MATCH (s:Symbol {name: $name}) "
            "WHERE s.unresolved IS NULL AND s.invalid_sha IS NULL "
            "MATCH (caller:File)-[c:CALLS|REFERENCES*1.." + str(depth) + "]->(s) "
            "WHERE caller.invalid_sha IS NULL "
            "RETURN DISTINCT caller.key, s.file, s.start, s.end, s.kind "
            "LIMIT 500"
        )
        rows = self.graph.query(q, {"name": symbol_name}).result_set
        return [
            {
                "caller": caller_key,
                "target_file": file_key,
                "target_start": start,
                "target_end": end,
                "target_kind": kind,
            }
            for caller_key, file_key, start, end, kind in rows
        ]

    def callees(self, symbol_name: str, depth: int = 1) -> list[dict[str, Any]]:
        """Symbols called from the file that defines ``symbol_name``.

        Definition's containing file is taken as the starting point;
        all outgoing CALLS edges (transitively up to ``depth``) are
        enumerated.
        """
        depth = max(1, min(depth, 3))
        q = (
            "MATCH (defFile:File)-[:DEFINES]->(s:Symbol {name: $name}) "
            "WHERE defFile.invalid_sha IS NULL AND s.invalid_sha IS NULL "
            "MATCH (defFile)-[:CALLS*1.." + str(depth) + "]->(target:Symbol) "
            "WHERE target.unresolved IS NULL AND target.invalid_sha IS NULL "
            "RETURN DISTINCT target.name, target.file, target.start, target.end, target.kind "
            "LIMIT 500"
        )
        rows = self.graph.query(q, {"name": symbol_name}).result_set
        return [
            {
                "name": name,
                "file": file_key,
                "start": start,
                "end": end,
                "kind": kind,
            }
            for name, file_key, start, end, kind in rows
        ]

    def importers(self, target: str) -> list[dict[str, Any]]:
        """Files that import a Module whose key matches ``target``.

        ``target`` may be a package name (``@acme-ng/security``,
        ``rxjs``) or a relative path that was preserved on ingest
        (``./bar``). Match is exact.
        """
        rows = self.graph.query(
            "MATCH (f:File)-[r:IMPORTS]->(m:Module {key: $key}) "
            "WHERE f.invalid_sha IS NULL AND r.invalid_sha IS NULL "
            "RETURN f.key, m.key",
            {"key": target},
        ).result_set
        return [{"file": f, "module": m} for f, m in rows]

    def dependencies(self, file_path: str, depth: int = 1) -> list[dict[str, Any]]:
        """Modules imported by ``file_path`` (forward IMPORTS).

        Depth>1 walks through *files* that the imported modules
        correspond to, but only those modules already linked in the
        graph; bare external packages don't have outgoing edges.
        """
        depth = max(1, min(depth, 3))
        q = (
            "MATCH (f:File {key: $key}) "
            "WHERE f.invalid_sha IS NULL "
            "MATCH (f)-[:IMPORTS*1.." + str(depth) + "]->(m:Module) "
            "WHERE m.invalid_sha IS NULL "
            "RETURN DISTINCT m.key"
        )
        rows = self.graph.query(q, {"key": file_path}).result_set
        return [{"module": m} for (m,) in rows]

    def definitions(self, symbol_name: str) -> list[dict[str, Any]]:
        """All files+line ranges that DEFINE a symbol with ``symbol_name``.

        Useful for disambiguation: tells the agent whether the name is
        unique (one row) or shared across files (multiple rows).
        """
        rows = self.graph.query(
            "MATCH (f:File)-[:DEFINES]->(s:Symbol {name: $name}) "
            "WHERE s.unresolved IS NULL "
            "  AND s.invalid_sha IS NULL "
            "  AND f.invalid_sha IS NULL "
            "RETURN f.key, s.start, s.end, s.kind",
            {"name": symbol_name},
        ).result_set
        return [
            {"file": f, "start": start, "end": end, "kind": kind}
            for f, start, end, kind in rows
        ]
