"""Snapshot tar.gz round-trip + verification."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_memory.sync.snapshot import (
    FORMAT_VERSION,
    Snapshot,
    SnapshotManifest,
    _canonical_digest,
    verify_snapshot,
)


def _make_snap(**overrides) -> Snapshot:
    vectors = [
        {"id": "v2", "vector": [0.0, 1.0], "payload": {"k": "b"}},
        {"id": "v1", "vector": [1.0, 0.0], "payload": {"k": "a"}},
    ]
    nodes = [
        {"label": "File", "key": "/x.py", "props": {"lang": "py"}},
        {"label": "Symbol", "key": "/x.py::f", "props": {"name": "f"}},
    ]
    edges = [
        {
            "type": "DEFINES",
            "src_label": "File",
            "src_key": "/x.py",
            "dst_label": "Symbol",
            "dst_key": "/x.py::f",
            "props": {},
        }
    ]
    state = {"last_sha": "abc123", "branch": "main"}
    digest = _canonical_digest(vectors, nodes, edges, state)
    manifest = SnapshotManifest(
        format_version=FORMAT_VERSION,
        project="demo",
        head_sha="abc123",
        branch="main",
        embed_model="bge-m3",
        embed_dim=2,
        created_at=0.0,
        created_by="t@t",
        tool_version="0.0.1",
        counts={"vectors": 2, "nodes": 2, "edges": 1},
        content_sha256=digest,
    )
    snap = Snapshot(
        manifest=manifest, vectors=vectors, nodes=nodes, edges=edges, state=state
    )
    for k, v in overrides.items():
        setattr(snap, k, v)
    return snap


def test_roundtrip_preserves_payload(tmp_path: Path) -> None:
    snap = _make_snap()
    out = snap.write(tmp_path / "snap.cmsnap")
    restored = Snapshot.read(out)
    assert restored.manifest == snap.manifest
    assert restored.vectors == snap.vectors
    assert restored.nodes == snap.nodes
    assert restored.edges == snap.edges
    assert restored.state == snap.state


def test_verify_detects_corruption(tmp_path: Path) -> None:
    snap = _make_snap()
    out = snap.write(tmp_path / "snap.cmsnap")
    # tamper: rewrite a vector after building
    snap.vectors[0]["vector"] = [9.0, 9.0]
    snap.write(out)
    # manifest still claims original digest -> mismatch
    result = verify_snapshot(out)
    assert not result.ok
    assert "digest mismatch" in (result.reason or "")


def test_verify_model_mismatch(tmp_path: Path) -> None:
    snap = _make_snap()
    out = snap.write(tmp_path / "snap.cmsnap")
    result = verify_snapshot(out, expected_model="other-model")
    assert not result.ok
    assert "embed_model" in (result.reason or "")


def test_verify_dim_mismatch(tmp_path: Path) -> None:
    snap = _make_snap()
    out = snap.write(tmp_path / "snap.cmsnap")
    result = verify_snapshot(out, expected_dim=4)
    assert not result.ok
    assert "embed_dim" in (result.reason or "")


def test_verify_clean_snapshot_ok(tmp_path: Path) -> None:
    snap = _make_snap()
    out = snap.write(tmp_path / "snap.cmsnap")
    result = verify_snapshot(out, expected_model="bge-m3", expected_dim=2)
    assert result.ok
    assert result.reason is None


def test_canonical_digest_order_independent() -> None:
    """Reordered input rows produce identical digest (canonical sort)."""
    a_vectors = [
        {"id": "a", "vector": [1.0], "payload": {}},
        {"id": "b", "vector": [2.0], "payload": {}},
    ]
    b_vectors = list(reversed(a_vectors))
    state: dict = {}
    assert _canonical_digest(a_vectors, [], [], state) == _canonical_digest(
        b_vectors, [], [], state
    )


@pytest.mark.parametrize("attr", ["vectors", "nodes", "edges"])
def test_canonical_digest_sensitive_to_content(attr: str) -> None:
    base_vectors = [{"id": "a", "vector": [1.0], "payload": {}}]
    base_nodes = [{"label": "File", "key": "/a", "props": {}}]
    base_edges = [
        {
            "type": "DEFINES",
            "src_label": "File",
            "src_key": "/a",
            "dst_label": "Symbol",
            "dst_key": "/a::f",
            "props": {},
        }
    ]
    state: dict = {}
    d1 = _canonical_digest(base_vectors, base_nodes, base_edges, state)
    # Mutate one element of the chosen list
    if attr == "vectors":
        v2 = [{"id": "a", "vector": [2.0], "payload": {}}]
        d2 = _canonical_digest(v2, base_nodes, base_edges, state)
    elif attr == "nodes":
        n2 = [{"label": "File", "key": "/a", "props": {"lang": "py"}}]
        d2 = _canonical_digest(base_vectors, n2, base_edges, state)
    else:
        e2 = [
            {
                "type": "CALLS",
                "src_label": "File",
                "src_key": "/a",
                "dst_label": "Symbol",
                "dst_key": "/a::f",
                "props": {},
            }
        ]
        d2 = _canonical_digest(base_vectors, base_nodes, e2, state)
    assert d1 != d2
