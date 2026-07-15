from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from falkordb import FalkorDB

from ..config import CONFIG

NodeLabel = str  # File | Symbol | Module
EdgeType = str  # IMPORTS | CALLS | DEFINES | EXPORTS

# Process-singleton FalkorDB registry, keyed by (host, port). A
# long-lived daemon constructs a fresh FalkorStore() per Pipeline() —
# without this cache that meant a fresh network connection per
# instance, never released. Distinct endpoints still get distinct
# connections.
_DB_LOCK = threading.Lock()
_DBS: dict[tuple[str, int], FalkorDB] = {}


def get_falkor_db(host: str, port: int) -> FalkorDB:
    """Process-singleton ``FalkorDB`` connection, keyed by ``(host, port)``.

    Multiple ``FalkorStore()`` instances pointed at the same endpoint
    share one underlying connection instead of each opening their own.

    Socket timeouts are mandatory: redis-py's default is ``None`` (block
    forever), so a half-open endpoint — a WSL2-forwarded port with no
    responder, or an endpoint security layer quarantining the first flow
    of a process — hangs every reingest/watch/MCP process indefinitely
    and they pile up. ``socket_timeout`` must exceed the server-side
    ``TIMEOUT_MAX`` (60 s, set below) so long graph queries are not cut
    short. The constructor is retried once because per-process traffic
    inspection can eat exactly the first connection and allow the next
    (observed: attempt 1 times out, attempt 2 answers instantly).
    """
    key = (host, port)
    with _DB_LOCK:
        db = _DBS.get(key)
        if db is None:
            last_exc: Exception | None = None
            for _ in range(2):
                try:
                    db = FalkorDB(
                        host=host,
                        port=port,
                        socket_connect_timeout=10,
                        socket_timeout=90,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 — retry once, then surface
                    last_exc = exc
                    db = None
            if db is None:
                assert last_exc is not None
                raise last_exc
            _DBS[key] = db
        return db


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
        self.db = get_falkor_db(
            host or CONFIG.falkor_host,
            port or CONFIG.falkor_port,
        )
        self._closed = False
        self.graph_name: str = graph_name or CONFIG.falkor_graph
        self.graph = self.db.select_graph(self.graph_name)
        # Two server-side tunables that bite on large graphs (e.g.
        # 200K-symbol / 270K-call monorepos):
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
        """Bulk-upsert nodes via ``UNWIND``; stamps temporal lifecycle when
        ``head_sha`` is provided.

        Previously this looped one ``MERGE`` query per node — a single
        ingested file with 50 symbols + imports + calls triggered 50
        Falkor round-trips. The UNWIND form collapses each label group
        into one query, cutting ingest wall time by an order of
        magnitude on real repos.

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
        by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for n in nodes:
            by_label[n.label].append({"key": n.key, "props": n.props})
        if not by_label:
            return

        for label, rows in by_label.items():
            if head_sha is None:
                self.graph.query(
                    f"""
                    UNWIND $rows AS row
                    MERGE (n:{label} {{key: row.key}})
                    SET n += row.props
                    """,
                    {"rows": rows},
                )
                continue
            self.graph.query(
                f"""
                UNWIND $rows AS row
                MERGE (n:{label} {{key: row.key}})
                ON CREATE SET n.first_seen_sha = $head,
                              n.last_seen_sha = $head,
                              n.first_seen_ord = $ord,
                              n.last_seen_ord = $ord
                ON MATCH  SET n.first_seen_sha = COALESCE(n.first_seen_sha, $head),
                              n.last_seen_sha = $head,
                              n.first_seen_ord = COALESCE(n.first_seen_ord, $ord),
                              n.last_seen_ord = $ord
                SET n += row.props
                SET n.invalid_sha = NULL,
                    n.invalid_ord = NULL,
                    n.invalid_at = NULL
                """,
                {"rows": rows, "head": head_sha, "ord": head_ord},
            )

    def upsert_edges(
        self,
        edges: Iterable[GraphEdge],
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
        """Bulk-upsert edges via ``UNWIND``; same temporal stamping as nodes.

        Edges group by ``(src_label, type, dst_label)`` because Cypher
        can't parameterize labels or relationship types. Each group
        becomes one query batch instead of one query per edge.
        """
        by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for e in edges:
            by_key[(e.src_label, e.type, e.dst_label)].append(
                {"src": e.src_key, "dst": e.dst_key, "props": e.props}
            )
        if not by_key:
            return

        for (src_label, etype, dst_label), rows in by_key.items():
            if head_sha is None:
                self.graph.query(
                    f"""
                    UNWIND $rows AS row
                    MERGE (a:{src_label} {{key: row.src}})
                    MERGE (b:{dst_label} {{key: row.dst}})
                    MERGE (a)-[r:{etype}]->(b)
                    SET r += row.props
                    """,
                    {"rows": rows},
                )
                continue
            self.graph.query(
                f"""
                UNWIND $rows AS row
                MERGE (a:{src_label} {{key: row.src}})
                MERGE (b:{dst_label} {{key: row.dst}})
                MERGE (a)-[r:{etype}]->(b)
                ON CREATE SET r.first_seen_sha = $head,
                              r.last_seen_sha = $head,
                              r.first_seen_ord = $ord,
                              r.last_seen_ord = $ord
                ON MATCH  SET r.first_seen_sha = COALESCE(r.first_seen_sha, $head),
                              r.last_seen_sha = $head,
                              r.first_seen_ord = COALESCE(r.first_seen_ord, $ord),
                              r.last_seen_ord = $ord
                SET r += row.props
                SET r.invalid_sha = NULL,
                    r.invalid_ord = NULL,
                    r.invalid_at = NULL
                """,
                {"rows": rows, "head": head_sha, "ord": head_ord},
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

    def count_symbols(self) -> int:
        """Return the total number of Symbol nodes in the graph.

        Returns 0 on any FalkorDB error (connection down, query timeout,
        etc.) so callers can degrade gracefully without breaking the
        ingest pipeline.
        """
        try:
            result = self.graph.query("MATCH (s:Symbol) RETURN count(s)")
            if result.result_set:
                return int(result.result_set[0][0])
            return 0
        except Exception:  # noqa: BLE001 — FalkorDB may be unreachable
            return 0

    def clear_graph(self) -> None:
        """Remove every node + edge in this project's graph."""
        self.graph.query("MATCH (n) DETACH DELETE n")

    def graph_exists(self, name: str) -> bool:
        """Return True if a FalkorDB graph named ``name`` exists.

        Best-effort: any error (connection down, unsupported command)
        returns False so callers can treat a missing graph as absent.
        """
        try:
            graphs: list[str] = self.db.list()
            return name in graphs
        except Exception:  # noqa: BLE001
            return False

    def drop_graph(self, name: str) -> None:
        """Delete the FalkorDB graph ``name`` if it exists.

        Best-effort: errors are swallowed so callers don't have to
        handle the case where the graph was never created (e.g. a clean
        environment before the first shadow rebuild).
        """
        try:
            self.db.connection.execute_command("GRAPH.DELETE", name)
        except Exception:  # noqa: BLE001
            pass

    def promote_shadow(self, shadow_graph_name: str) -> None:
        """Atomically replace the live graph with the shadow graph.

        Executes in this strict order (the unit test asserts the call
        sequence):

        1. ``GRAPH.DELETE self.graph_name`` — clear the destination so
           FalkorDB's GRAPH.COPY doesn't error on an existing target.
        2. ``GRAPH.COPY shadow_graph_name self.graph_name`` — copy the
           fully-built shadow into the live graph name.
        3. ``GRAPH.DELETE shadow_graph_name`` — clean up the shadow.
        4. Rebind ``self.graph`` to the freshly-copied graph.

        If the GRAPH.COPY step fails (e.g. FalkorDB is down) the
        exception propagates immediately — the live graph is gone at
        that point but the shadow is still intact so the caller can
        retry. The DELETE steps are best-effort calls but are kept as
        direct ``execute_command`` invocations so the test's call-order
        assertion holds.
        """
        self.db.connection.execute_command("GRAPH.DELETE", self.graph_name)
        # Let a COPY failure propagate — shadow is intact for retry.
        self.db.connection.execute_command(
            "GRAPH.COPY", shadow_graph_name, self.graph_name
        )
        try:
            self.db.connection.execute_command("GRAPH.DELETE", shadow_graph_name)
        except Exception:  # noqa: BLE001 — shadow cleanup is best-effort
            pass
        self.graph = self.db.select_graph(self.graph_name)

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

        ``depth > 1`` recurses in Python: each caller File is treated as
        a new target by walking the symbols it DEFINES. A pure Cypher
        variable-length path doesn't work because CALLS/REFERENCES go
        File→Symbol only — the graph has no reverse edge to chain on.
        """
        depth = max(1, min(depth, 3))
        seen_callers: set[str] = set()
        seen_names: set[str] = set()
        out: list[dict[str, Any]] = []
        frontier_names: list[str] = [symbol_name]
        for hop in range(depth):
            next_names: list[str] = []
            ring_callers: list[str] = []
            for name in frontier_names:
                if name in seen_names:
                    continue
                seen_names.add(name)
                for row in self._callers_one_hop(name):
                    if row["caller"] in seen_callers:
                        continue
                    seen_callers.add(row["caller"])
                    out.append(row)
                    ring_callers.append(row["caller"])
            if hop + 1 >= depth or not ring_callers:
                break
            for caller_file in ring_callers:
                next_names.extend(self._defines_at(caller_file))
            frontier_names = next_names
        return out

    def _callers_one_hop(self, symbol_name: str) -> list[dict[str, Any]]:
        rows = self.graph.query(
            "MATCH (s:Symbol {name: $name}) "
            "WHERE s.unresolved IS NULL AND s.invalid_sha IS NULL "
            "MATCH (caller:File)-[c:CALLS|REFERENCES]->(s) "
            "WHERE caller.invalid_sha IS NULL "
            "RETURN DISTINCT caller.key, s.file, s.start, s.end, s.kind "
            "LIMIT 500",
            {"name": symbol_name},
        ).result_set
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

    def _defines_at(self, file_key: str) -> list[str]:
        """Names of resolved symbols defined by ``file_key``."""
        rows = self.graph.query(
            "MATCH (f:File {key: $key})-[:DEFINES]->(s:Symbol) "
            "WHERE s.unresolved IS NULL AND s.invalid_sha IS NULL "
            "  AND f.invalid_sha IS NULL "
            "RETURN DISTINCT s.name LIMIT 500",
            {"key": file_key},
        ).result_set
        return [name for (name,) in rows if name]

    def callees(self, symbol_name: str, depth: int = 1) -> list[dict[str, Any]]:
        """Callees reachable from the file that defines ``symbol_name``.

        Returns both **resolved** targets (a Symbol or Type node the
        resolver bound the call to) and **unresolved** placeholders.
        Hiding placeholders silently turned ``callees`` into a no-op
        for Angular clean-arch use cases where every call goes through
        ``this.port.method()`` and the bare method name can't be bound
        to a unique definition — the agent saw an empty list with no
        signal that calls actually exist.

        ``depth > 1`` recurses in Python by walking through DEFINES
        edges of each discovered callee. A pure Cypher variable-length
        path doesn't work here: CALLS goes File→Symbol only, so the
        graph has no Symbol→File reverse to chain on.
        """
        depth = max(1, min(depth, 3))
        seen: set[tuple[str, str | None]] = set()
        out: list[dict[str, Any]] = []
        frontier_files: list[str | None] = [None]  # None = start from defining file
        frontier_symbol: str | None = symbol_name
        for _ in range(depth):
            rows = self._callees_one_hop(frontier_symbol, frontier_files)
            next_files: list[str | None] = []
            for row in rows:
                key = (row["name"], row["file"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
                if row["resolved"] and row["file"]:
                    next_files.append(row["file"])
            if not next_files:
                break
            frontier_symbol = None
            frontier_files = next_files
        return out

    def _callees_one_hop(
        self, symbol_name: str | None, files: list[str | None]
    ) -> list[dict[str, Any]]:
        """One CALLS hop. Either anchored by the symbol's defining file
        (``symbol_name`` set, ``files`` ignored) or by a list of explicit
        file keys (``symbol_name`` None)."""
        if symbol_name is not None:
            q = (
                "MATCH (defFile:File)-[:DEFINES]->(s:Symbol {name: $name}) "
                "WHERE defFile.invalid_sha IS NULL AND s.invalid_sha IS NULL "
                "MATCH (defFile)-[:CALLS]->(target) "
                "WHERE target.invalid_sha IS NULL "
                "  AND (labels(target)[0] = 'Symbol' OR labels(target)[0] = 'Type') "
                "RETURN DISTINCT target.name, target.file, target.start, "
                "  target.end, target.kind, target.unresolved, labels(target)[0] "
                "LIMIT 500"
            )
            params: dict[str, Any] = {"name": symbol_name}
        else:
            q = (
                "MATCH (defFile:File) WHERE defFile.key IN $files "
                "  AND defFile.invalid_sha IS NULL "
                "MATCH (defFile)-[:CALLS]->(target) "
                "WHERE target.invalid_sha IS NULL "
                "  AND (labels(target)[0] = 'Symbol' OR labels(target)[0] = 'Type') "
                "RETURN DISTINCT target.name, target.file, target.start, "
                "  target.end, target.kind, target.unresolved, labels(target)[0] "
                "LIMIT 500"
            )
            params = {"files": [f for f in files if f]}
        rows = self.graph.query(q, params).result_set
        return [
            {
                "name": name,
                "file": file_key,
                "start": start,
                "end": end,
                "kind": kind,
                "resolved": unresolved is None,
                "label": label,
            }
            for name, file_key, start, end, kind, unresolved, label in rows
        ]

    def injects(self, symbol_name: str) -> list[dict[str, Any]]:
        """DI dependencies declared in the file that defines ``symbol_name``.

        Angular's ``inject(Token)`` primitive (and Razor's ``@inject``)
        emit INJECTS edges separately from CALLS so the DI graph isn't
        conflated with raw method invocation. Use this to answer
        "what does this class depend on?" without sifting through
        imported modules.
        """
        rows = self.graph.query(
            "MATCH (defFile:File)-[:DEFINES]->(s:Symbol {name: $name}) "
            "WHERE defFile.invalid_sha IS NULL AND s.invalid_sha IS NULL "
            "MATCH (defFile)-[:INJECTS]->(target) "
            "WHERE target.invalid_sha IS NULL "
            "RETURN DISTINCT target.name, target.key, target.file, "
            "  target.kind, target.unresolved "
            "LIMIT 500",
            {"name": symbol_name},
        ).result_set
        return [
            {
                "name": name,
                "key": key,
                "file": file_key,
                "kind": kind,
                "resolved": unresolved is None,
            }
            for name, key, file_key, kind, unresolved in rows
        ]

    def injectors(self, token: str) -> list[dict[str, Any]]:
        """Files that inject ``token`` (reverse INJECTS edges).

        ``token`` may be the bare name of a class/abstract class used as
        an Angular DI token, or any symbol exposed via INJECTS.
        """
        rows = self.graph.query(
            "MATCH (f:File)-[:INJECTS]->(s:Symbol {name: $name}) "
            "WHERE f.invalid_sha IS NULL AND s.invalid_sha IS NULL "
            "RETURN DISTINCT f.key LIMIT 500",
            {"name": token},
        ).result_set
        return [{"file": file_key} for (file_key,) in rows]

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

    def close(self) -> None:
        """Best-effort, idempotent teardown.

        ``self.db`` is a process-wide singleton (see ``get_falkor_db``),
        so closing its connection here only matters when a caller
        explicitly wants to release it (tests, short-lived CLI
        invocations). Swallows any error — a shared connection with no
        ``close()``, or one that raises, must never break teardown.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self.db.connection.close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
