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
