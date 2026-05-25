"""End-to-end snapshot dump → apply round-trip against a real Qdrant.

This guards a class of bug that the format-only tests missed:
``_dump_vectors`` used to do ``list(p.vector)`` which silently
discarded the actual embedding when Qdrant returned the hybrid layout
(a dict keyed by named vector slot). Snapshots looked valid but
contained the string ``["dense"]`` where the embedding should have
been; ``apply_snapshot`` then crashed in ``QdrantStore.upsert`` trying
to access ``vector.dense`` on a list of slot names.

These tests stand up a real FalkorDB + Qdrant via the module
infrastructure, write a synthetic point with the hybrid layout,
dump → apply, and verify the vector is byte-for-byte preserved.
"""

from __future__ import annotations

import uuid

import pytest

from code_memory.config import CONFIG
from code_memory.embed.m3 import HybridVec, SparseVec
from code_memory.graph.falkor_store import FalkorStore
from code_memory.sync.snapshot import (
    _hybridvec_from_dump,
    _normalize_vector_for_dump,
    apply_snapshot,
    build_snapshot,
)
from code_memory.vector import QdrantStore, VectorRecord


def _stores_available() -> bool:
    try:
        QdrantStore().client.get_collections()
        s = FalkorStore(graph_name="cm_snapshot_e2e_probe")
        s.graph.query("RETURN 1")
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _stores_available(),
    reason="FalkorDB / Qdrant not reachable; skip live e2e",
)


# ---------------------------------------------------------------- pure-format


def test_normalize_handles_hybrid_dict() -> None:
    raw = {
        "dense": [0.1, 0.2, 0.3],
        "sparse": SparseVec(indices=[1, 5], values=[0.5, 0.7]),
    }
    out = _normalize_vector_for_dump(raw)
    assert out["dense"] == pytest.approx([0.1, 0.2, 0.3])
    assert out["sparse"] == {"indices": [1, 5], "values": [0.5, 0.7]}


def test_normalize_handles_legacy_bare_list() -> None:
    out = _normalize_vector_for_dump([0.1, 0.2, 0.3])
    assert out == {"dense": [0.1, 0.2, 0.3]}


def test_normalize_returns_empty_for_none() -> None:
    assert _normalize_vector_for_dump(None) == {}


def test_normalize_drops_empty_sparse() -> None:
    raw = {
        "dense": [0.1],
        "sparse": SparseVec(indices=[], values=[]),
    }
    out = _normalize_vector_for_dump(raw)
    assert "sparse" not in out  # no point keeping an empty sparse


def test_hybridvec_from_dump_roundtrips_dense_only() -> None:
    raw = {"dense": [0.1, 0.2, 0.3]}
    vec = _hybridvec_from_dump(raw)
    assert vec.dense == [0.1, 0.2, 0.3]
    assert vec.sparse.indices == []


def test_hybridvec_from_dump_roundtrips_dense_and_sparse() -> None:
    raw = {
        "dense": [0.1, 0.2],
        "sparse": {"indices": [1, 4], "values": [0.5, 0.7]},
    }
    vec = _hybridvec_from_dump(raw)
    assert vec.dense == [0.1, 0.2]
    assert vec.sparse.indices == [1, 4]
    assert vec.sparse.values == [0.5, 0.7]


def test_hybridvec_from_dump_handles_legacy_bare_list() -> None:
    """Old snapshots wrote ``vector: [floats]`` not a dict — still load."""
    vec = _hybridvec_from_dump([0.1, 0.2, 0.3])
    assert vec.dense == [0.1, 0.2, 0.3]
    assert vec.sparse.indices == []


# ------------------------------------------------------------ live round-trip


@pytest.fixture()
def isolated_project():
    """Project slug + auto-cleanup of the resulting Qdrant/Falkor state."""
    slug = f"cm-snap-e2e-{uuid.uuid4().hex[:8]}"
    cfg = CONFIG.for_project(slug)
    yield slug, cfg
    # cleanup
    try:
        QdrantStore().client.delete_collection(cfg.qdrant_code)
    except Exception:  # noqa: BLE001
        pass
    try:
        FalkorStore(graph_name=cfg.falkor_graph).clear_graph()
    except Exception:  # noqa: BLE001
        pass


def test_snapshot_roundtrip_preserves_dense_vector(isolated_project) -> None:
    slug, cfg = isolated_project
    qs = QdrantStore()
    qs.ensure_collection(cfg.qdrant_code)

    # Seed the source collection with a known vector. The dim has to
    # match QdrantStore's resolved dimension (CONFIG.embed_dim defaults
    # to the sentinel 0; QdrantStore turns that into the model's real
    # dim via resolve_embed_dim).
    dim = qs.dim
    original = HybridVec(
        dense=[0.1, 0.2, 0.3] + [0.0] * (dim - 3),
        sparse=SparseVec(indices=[], values=[]),
    )
    point_id = str(uuid.uuid4())
    qs.upsert(
        cfg.qdrant_code,
        [VectorRecord(id=point_id, vector=original, payload={"path": "a.py"})],
    )

    # Dump → apply into the same project (recreate collection in between
    # to prove we're rebuilding from the snapshot, not reading stale
    # state).
    # Read back the original to capture Qdrant's internal normalisation
    # (Cosine collections normalise vectors at insertion time). The
    # roundtrip assertion compares what Qdrant returns BEFORE wipe to
    # what it returns AFTER apply.
    pre_points, _ = qs.client.scroll(
        collection_name=cfg.qdrant_code,
        limit=10,
        with_vectors=True,
        with_payload=True,
    )
    pre_dense = pre_points[0].vector["dense"]

    snap = build_snapshot(project=slug, head_sha="deadbeef", branch=None)
    assert snap.manifest.counts["vectors"] == 1
    # The dumped vector must carry the actual dense floats, not just
    # the dict keys — that was the original bug.
    dumped = snap.vectors[0]["vector"]
    assert isinstance(dumped, dict) and "dense" in dumped, (
        "dumped vector lost its dense floats (regression of the bug "
        "where _dump_vectors did list(p.vector))"
    )
    assert dumped["dense"] == pytest.approx(pre_dense)

    # Wipe + apply.
    qs.recreate_collection(cfg.qdrant_code)
    apply_snapshot(snap, cfg=cfg)

    # Re-read and compare against the pre-wipe value.
    points, _ = qs.client.scroll(
        collection_name=cfg.qdrant_code,
        limit=10,
        with_vectors=True,
        with_payload=True,
    )
    assert len(points) == 1
    restored = points[0].vector
    assert restored["dense"] == pytest.approx(pre_dense)
    assert points[0].payload["path"] == "a.py"
