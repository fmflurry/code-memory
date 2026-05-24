from __future__ import annotations

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

    def ensure_indexes(self) -> None:
        for label in ("File", "Symbol", "Module"):
            try:
                self.graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.key)")
            except Exception:
                # index may already exist
                pass

    def upsert_nodes(self, nodes: Iterable[GraphNode]) -> None:
        for n in nodes:
            self.graph.query(
                f"MERGE (n:{n.label} {{key: $key}}) SET n += $props",
                {"key": n.key, "props": n.props},
            )

    def upsert_edges(self, edges: Iterable[GraphEdge]) -> None:
        for e in edges:
            self.graph.query(
                f"""
                MERGE (a:{e.src_label} {{key: $src}})
                MERGE (b:{e.dst_label} {{key: $dst}})
                MERGE (a)-[r:{e.type}]->(b)
                SET r += $props
                """,
                {"src": e.src_key, "dst": e.dst_key, "props": e.props},
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

    def delete_file(self, path: str) -> None:
        self.graph.query(
            "MATCH (f:File {key: $key}) DETACH DELETE f",
            {"key": path},
        )

    def clear_graph(self) -> None:
        """Remove every node + edge in this project's graph."""
        self.graph.query("MATCH (n) DETACH DELETE n")

    # ------------------------------------------------------------------
    # Topology queries — exposed via MCP/CLI as `codememory_<op>` tools.
    # All return ``list[dict]`` so callers can render or JSON-serialize.
    # ``depth`` is capped at 3 to keep traversal bounded.

    def callers(self, symbol_name: str, depth: int = 1) -> list[dict[str, Any]]:
        """Files (and their symbols) that call ``symbol_name``.

        Returns one row per direct caller file; symbol coordinates of
        the called definition are included so the user can jump to it.
        """
        depth = max(1, min(depth, 3))
        q = (
            "MATCH (s:Symbol {name: $name}) "
            "WHERE s.unresolved IS NULL "
            "MATCH (caller:File)-[c:CALLS*1.." + str(depth) + "]->(s) "
            "RETURN DISTINCT caller.key, s.file, s.start, s.end, s.kind"
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
            "MATCH (defFile:File)-[:DEFINES]->(:Symbol {name: $name}) "
            "MATCH (defFile)-[:CALLS*1.." + str(depth) + "]->(target:Symbol) "
            "WHERE target.unresolved IS NULL "
            "RETURN DISTINCT target.name, target.file, target.start, target.end, target.kind"
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
            "MATCH (f:File)-[:IMPORTS]->(m:Module {key: $key}) "
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
            "MATCH (f:File {key: $key})-[:IMPORTS*1.." + str(depth) + "]->(m:Module) "
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
            "RETURN f.key, s.start, s.end, s.kind",
            {"name": symbol_name},
        ).result_set
        return [
            {"file": f, "start": start, "end": end, "kind": kind}
            for f, start, end, kind in rows
        ]
