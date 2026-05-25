"""Snapshot blob format: build, verify, apply.

Layout of a ``<sha>.cmsnap`` tar.gz archive::

    manifest.json
    vectors/code.jsonl       # one point per line: {id, vector, payload}
    graph/nodes.jsonl        # {label, key, props}
    graph/edges.jsonl        # {type, src_label, src_key, dst_label, dst_key, props}
    state.json               # {last_sha, last_ts, branch}

Snapshots are content-addressed: filename = git SHA of the commit they
represent. ``manifest.content_sha256`` is the digest of the canonical
concatenation of the four jsonl/json payloads so two builds on the same
SHA produce identical bytes when extractor + embedder are deterministic.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import platform
import tarfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..config import CONFIG, Config
from ..graph.falkor_store import FalkorStore, GraphEdge, GraphNode
from ..embed.m3 import HybridVec, SparseVec
from ..vector.qdrant_store import QdrantStore, VectorRecord

FORMAT_VERSION = 1
DEFAULT_BATCH = 256


@dataclass(frozen=True)
class SnapshotManifest:
    format_version: int
    project: str
    head_sha: str
    branch: str | None
    embed_model: str
    embed_dim: int
    created_at: float
    created_by: str
    tool_version: str
    counts: dict[str, int]
    content_sha256: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, data: str) -> SnapshotManifest:
        obj = json.loads(data)
        return cls(**obj)


@dataclass
class Snapshot:
    """In-memory representation. Use ``write()`` to materialise a tar.gz."""

    manifest: SnapshotManifest
    vectors: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, "w:gz") as tar:
            _add(tar, "manifest.json", self.manifest.to_json().encode())
            _add(tar, "vectors/code.jsonl", _jsonl(self.vectors))
            _add(tar, "graph/nodes.jsonl", _jsonl(self.nodes))
            _add(tar, "graph/edges.jsonl", _jsonl(self.edges))
            _add(tar, "state.json", json.dumps(self.state, sort_keys=True).encode())
        return path

    @classmethod
    def read(cls, path: Path) -> Snapshot:
        with tarfile.open(path, "r:gz") as tar:
            manifest = SnapshotManifest.from_json(_extract(tar, "manifest.json").decode())
            vectors = list(_read_jsonl(_extract(tar, "vectors/code.jsonl")))
            nodes = list(_read_jsonl(_extract(tar, "graph/nodes.jsonl")))
            edges = list(_read_jsonl(_extract(tar, "graph/edges.jsonl")))
            state = json.loads(_extract(tar, "state.json").decode() or "{}")
        return cls(manifest=manifest, vectors=vectors, nodes=nodes, edges=edges, state=state)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_snapshot(
    *,
    project: str,
    head_sha: str,
    branch: str | None,
    cfg: Config | None = None,
    vector: QdrantStore | None = None,
    graph: FalkorStore | None = None,
    state: dict[str, Any] | None = None,
    tool_version: str = "0.1.0",
    created_by: str | None = None,
) -> Snapshot:
    """Dump live stores for ``project`` into an in-memory ``Snapshot``."""
    cfg = cfg or CONFIG.for_project(project)
    vector = vector or QdrantStore()
    graph = graph or FalkorStore(graph_name=cfg.falkor_graph)

    vectors = list(_dump_vectors(vector, cfg.qdrant_code))
    nodes, edges = _dump_graph(graph)
    state = state or {}

    counts = {
        "vectors": len(vectors),
        "nodes": len(nodes),
        "edges": len(edges),
    }
    digest = _canonical_digest(vectors, nodes, edges, state)
    manifest = SnapshotManifest(
        format_version=FORMAT_VERSION,
        project=project,
        head_sha=head_sha,
        branch=branch,
        embed_model=cfg.embed_model,
        embed_dim=cfg.embed_dim,
        created_at=time.time(),
        created_by=created_by or _default_creator(),
        tool_version=tool_version,
        counts=counts,
        content_sha256=digest,
    )
    return Snapshot(
        manifest=manifest, vectors=vectors, nodes=nodes, edges=edges, state=state
    )


def _dump_vectors(store: QdrantStore, collection: str) -> Iterator[dict[str, Any]]:
    """Page through every point in the collection via Qdrant scroll API.

    The hybrid Qdrant layout returns ``p.vector`` as a dict keyed by
    named vector slot (``dense`` and optionally ``sparse``). The legacy
    layout returns a bare list of floats. We serialise both forms into
    a single normalised JSON shape ``{"dense": [...], "sparse":
    {"indices": [...], "values": [...]}}`` so the apply path doesn't
    have to branch on layout era. Previously this used ``list(p.vector)``,
    which silently turned the dict into a list of slot names —
    discarding every actual embedding and producing snapshots that
    couldn't round-trip.
    """
    try:
        store.ensure_collection(collection)
    except Exception:
        return
    offset: Any = None
    while True:
        try:
            points, next_offset = store.client.scroll(
                collection_name=collection,
                limit=DEFAULT_BATCH,
                offset=offset,
                with_vectors=True,
                with_payload=True,
            )
        except Exception:
            return
        for p in points:
            yield {
                "id": str(p.id),
                "vector": _normalize_vector_for_dump(p.vector),
                "payload": dict(p.payload or {}),
            }
        if next_offset is None:
            return
        offset = next_offset


def _hybridvec_from_dump(payload: Any) -> HybridVec:
    """Reverse of :func:`_normalize_vector_for_dump`.

    Accepts both the new normalised dict shape (``{"dense": [...],
    "sparse": {...}}``) and three legacy shapes that may sit in older
    snapshots: a bare list of floats, a dict with only ``dense``, or
    an empty dict. Always returns a :class:`HybridVec`; sparse is
    empty when the snapshot didn't carry it (matches the Ollama /
    TEI dense-only invariant).
    """
    if isinstance(payload, list):
        return HybridVec(
            dense=[float(x) for x in payload],
            sparse=SparseVec(indices=[], values=[]),
        )
    if not isinstance(payload, dict):
        return HybridVec(dense=[], sparse=SparseVec(indices=[], values=[]))
    dense = [float(x) for x in payload.get("dense") or []]
    sp = payload.get("sparse") or {}
    sparse = SparseVec(
        indices=[int(i) for i in sp.get("indices") or []],
        values=[float(v) for v in sp.get("values") or []],
    )
    return HybridVec(dense=dense, sparse=sparse)


def _normalize_vector_for_dump(vec: Any) -> dict[str, Any]:
    """Coerce any Qdrant vector return shape into the dump JSON shape.

    * Hybrid layout: ``{"dense": [...], "sparse": SparseVector(...)}``
      → ``{"dense": [...], "sparse": {"indices": [...], "values": [...]}}``.
    * Legacy single-vector layout: ``[float, ...]`` → ``{"dense": [...]}``.
    * Missing / None: ``{}`` (downstream filters empties).
    """
    if vec is None:
        return {}
    if isinstance(vec, dict):
        out: dict[str, Any] = {}
        dense = vec.get("dense")
        if dense is not None:
            out["dense"] = [float(x) for x in dense]
        sparse = vec.get("sparse")
        if sparse is not None:
            # Qdrant's SparseVector exposes ``indices`` and ``values``;
            # some client versions return a plain dict. Handle both.
            indices = getattr(sparse, "indices", None)
            values = getattr(sparse, "values", None)
            if indices is None and isinstance(sparse, dict):
                indices = sparse.get("indices", [])
                values = sparse.get("values", [])
            if indices:
                out["sparse"] = {
                    "indices": [int(i) for i in indices],
                    "values": [float(v) for v in (values or [])],
                }
        return out
    # Legacy: bare list of floats.
    return {"dense": [float(x) for x in vec]}


def _dump_graph(store: FalkorStore) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read every node + edge from the project graph."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    try:
        node_rows = store.graph.query(
            "MATCH (n) RETURN labels(n) AS labels, n.key AS key, n AS node"
        ).result_set
    except Exception:
        node_rows = []
    for labels, key, node in node_rows:
        label = labels[0] if labels else "Node"
        props = dict(node.properties) if hasattr(node, "properties") else {}
        nodes.append({"label": label, "key": key, "props": props})

    try:
        edge_rows = store.graph.query(
            "MATCH (a)-[r]->(b) "
            "RETURN type(r) AS t, labels(a) AS sl, a.key AS sk, "
            "labels(b) AS dl, b.key AS dk, r AS edge"
        ).result_set
    except Exception:
        edge_rows = []
    for t, sl, sk, dl, dk, edge in edge_rows:
        props = dict(edge.properties) if hasattr(edge, "properties") else {}
        edges.append(
            {
                "type": t,
                "src_label": sl[0] if sl else "Node",
                "src_key": sk,
                "dst_label": dl[0] if dl else "Node",
                "dst_key": dk,
                "props": props,
            }
        )
    return nodes, edges


# ---------------------------------------------------------------------------
# Apply (restore into live stores)
# ---------------------------------------------------------------------------


def apply_snapshot(
    snap: Snapshot,
    *,
    cfg: Config | None = None,
    vector: QdrantStore | None = None,
    graph: FalkorStore | None = None,
) -> dict[str, int]:
    """Wipe and restore vectors + graph for the snapshot's project.

    Caller is responsible for verifying ``model_version`` compatibility
    *before* invoking this — embeddings from one model cannot be reused
    with another and a mismatched apply will corrupt retrieval results.
    """
    cfg = cfg or CONFIG.for_project(snap.manifest.project)
    vector = vector or QdrantStore()
    graph = graph or FalkorStore(graph_name=cfg.falkor_graph)

    # vectors
    vector.recreate_collection(cfg.qdrant_code)
    if snap.vectors:
        records = [
            VectorRecord(
                id=v["id"],
                vector=_hybridvec_from_dump(v.get("vector")),
                payload=v.get("payload") or {},
            )
            for v in snap.vectors
            if v.get("vector")
        ]
        # batched upsert to avoid huge single requests
        for i in range(0, len(records), DEFAULT_BATCH):
            vector.upsert(cfg.qdrant_code, records[i : i + DEFAULT_BATCH])

    # graph
    graph.clear_graph()
    graph.ensure_indexes()
    if snap.nodes:
        graph.upsert_nodes(
            GraphNode(label=n["label"], key=n["key"], props=n.get("props") or {})
            for n in snap.nodes
        )
    if snap.edges:
        graph.upsert_edges(
            GraphEdge(
                type=e["type"],
                src_label=e["src_label"],
                src_key=e["src_key"],
                dst_label=e["dst_label"],
                dst_key=e["dst_key"],
                props=e.get("props") or {},
            )
            for e in snap.edges
        )

    return {
        "vectors": len(snap.vectors),
        "nodes": len(snap.nodes),
        "edges": len(snap.edges),
    }


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str | None
    manifest: SnapshotManifest


def verify_snapshot(
    path: Path | None = None,
    snap: Snapshot | None = None,
    *,
    expected_model: str | None = None,
    expected_dim: int | None = None,
) -> VerifyResult:
    """Recompute content digest and check format/model compatibility."""
    if snap is None:
        if path is None:
            raise ValueError("verify_snapshot requires path or snap")
        snap = Snapshot.read(path)
    m = snap.manifest
    if m.format_version != FORMAT_VERSION:
        return VerifyResult(
            False, f"format_version mismatch (got {m.format_version})", m
        )
    if expected_model and m.embed_model != expected_model:
        return VerifyResult(
            False,
            f"embed_model mismatch (snapshot={m.embed_model} local={expected_model})",
            m,
        )
    if expected_dim and m.embed_dim != expected_dim:
        return VerifyResult(
            False,
            f"embed_dim mismatch (snapshot={m.embed_dim} local={expected_dim})",
            m,
        )
    digest = _canonical_digest(snap.vectors, snap.nodes, snap.edges, snap.state)
    if digest != m.content_sha256:
        return VerifyResult(False, "content digest mismatch (corruption?)", m)
    return VerifyResult(True, None, m)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonical_digest(
    vectors: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    state: dict[str, Any],
) -> str:
    h = hashlib.sha256()
    for v in sorted(vectors, key=lambda x: x.get("id", "")):
        h.update(_canon(v).encode())
    for n in sorted(nodes, key=lambda x: (x.get("label", ""), x.get("key", ""))):
        h.update(_canon(n).encode())
    for e in sorted(
        edges,
        key=lambda x: (x.get("type", ""), x.get("src_key", ""), x.get("dst_key", "")),
    ):
        h.update(_canon(e).encode())
    h.update(_canon(state).encode())
    return h.hexdigest()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    for r in rows:
        buf.write(json.dumps(r, sort_keys=True, separators=(",", ":")).encode())
        buf.write(b"\n")
    return buf.getvalue()


def _read_jsonl(blob: bytes) -> Iterator[dict[str, Any]]:
    for line in blob.splitlines():
        if not line.strip():
            continue
        yield json.loads(line)


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0  # deterministic
    tar.addfile(info, io.BytesIO(data))


def _extract(tar: tarfile.TarFile, name: str) -> bytes:
    member = tar.getmember(name)
    f = tar.extractfile(member)
    if f is None:
        return b""
    return f.read()


def _default_creator() -> str:
    import os

    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    return f"{user}@{platform.node()}"


# silence unused import warning in some linters
_ = gzip
