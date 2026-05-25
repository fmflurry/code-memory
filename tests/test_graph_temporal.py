"""Temporal stamping + tombstoning on FalkorStore.

Live tests against the local FalkorDB container. Skipped if the daemon
is not reachable so unit-test runs without infra still pass.
"""

from __future__ import annotations

import uuid

import pytest

from code_memory.config import CONFIG
from code_memory.graph.falkor_store import FalkorStore, GraphEdge, GraphNode


def _falkor_available() -> bool:
    try:
        s = FalkorStore(graph_name="cm_temporal_probe")
        s.graph.query("RETURN 1")
        return True
    except Exception:  # noqa: BLE001 - any failure means no daemon
        return False


pytestmark = pytest.mark.skipif(
    not _falkor_available(), reason="FalkorDB not reachable on configured host"
)


@pytest.fixture()
def store() -> FalkorStore:
    name = f"cm_temporal_{uuid.uuid4().hex[:8]}"
    s = FalkorStore(graph_name=name)
    s.ensure_indexes()
    yield s
    s.graph.query("MATCH (n) DETACH DELETE n")


def test_upsert_node_stamps_first_and_last_seen(store: FalkorStore) -> None:
    store.upsert_nodes(
        [GraphNode(label="File", key="/a.ts", props={"lang": "typescript"})],
        head_sha="sha-1",
    )
    rows = store.graph.query(
        "MATCH (f:File {key:'/a.ts'}) RETURN f.first_seen_sha, f.last_seen_sha, f.invalid_sha"
    ).result_set
    assert rows == [["sha-1", "sha-1", None]]


def test_re_upsert_advances_last_seen_only(store: FalkorStore) -> None:
    store.upsert_nodes(
        [GraphNode(label="File", key="/a.ts", props={"lang": "typescript"})],
        head_sha="sha-1",
    )
    store.upsert_nodes(
        [GraphNode(label="File", key="/a.ts", props={"lang": "typescript"})],
        head_sha="sha-2",
    )
    rows = store.graph.query(
        "MATCH (f:File {key:'/a.ts'}) RETURN f.first_seen_sha, f.last_seen_sha"
    ).result_set
    assert rows == [["sha-1", "sha-2"]]


def test_delete_file_with_head_sha_tombstones(store: FalkorStore) -> None:
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/a.ts", props={"lang": "typescript"}),
            GraphNode(label="Symbol", key="/a.ts::foo#1", props={"name": "foo"}),
        ],
        head_sha="sha-1",
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/a.ts",
                dst_label="Symbol",
                dst_key="/a.ts::foo#1",
            )
        ],
        head_sha="sha-1",
    )
    store.delete_file("/a.ts", head_sha="sha-2")
    rows = store.graph.query(
        "MATCH (f:File {key:'/a.ts'}) RETURN f.invalid_sha"
    ).result_set
    assert rows == [["sha-2"]]
    sym_rows = store.graph.query(
        "MATCH (s:Symbol {key:'/a.ts::foo#1'}) RETURN s.invalid_sha"
    ).result_set
    assert sym_rows == [["sha-2"]]


def test_delete_file_without_head_sha_hard_deletes(store: FalkorStore) -> None:
    store.upsert_nodes(
        [GraphNode(label="File", key="/a.ts", props={})],
    )
    store.delete_file("/a.ts")  # no head_sha → legacy hard delete
    rows = store.graph.query("MATCH (f:File {key:'/a.ts'}) RETURN f").result_set
    assert rows == []


def test_reupsert_after_tombstone_revives(store: FalkorStore) -> None:
    store.upsert_nodes(
        [GraphNode(label="File", key="/a.ts", props={})],
        head_sha="sha-1",
    )
    store.delete_file("/a.ts", head_sha="sha-2")
    store.upsert_nodes(
        [GraphNode(label="File", key="/a.ts", props={})],
        head_sha="sha-3",
    )
    rows = store.graph.query(
        "MATCH (f:File {key:'/a.ts'}) RETURN f.first_seen_sha, f.last_seen_sha, f.invalid_sha"
    ).result_set
    assert rows == [["sha-1", "sha-3", None]]


def test_definitions_filters_tombstoned(store: FalkorStore) -> None:
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/a.ts", props={}),
            GraphNode(label="Symbol", key="/a.ts::foo#1", props={"name": "foo", "start": 1, "end": 5, "kind": "function"}),
        ],
        head_sha="sha-1",
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/a.ts",
                dst_label="Symbol",
                dst_key="/a.ts::foo#1",
            )
        ],
        head_sha="sha-1",
    )
    assert len(store.definitions("foo")) == 1
    store.delete_file("/a.ts", head_sha="sha-2")
    assert store.definitions("foo") == []


def test_drift_lists_stale_and_tombstoned(store: FalkorStore) -> None:
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/a.ts", props={}),
            GraphNode(label="Symbol", key="/a.ts::foo#1", props={"name": "foo"}),
            GraphNode(label="Symbol", key="/a.ts::bar#1", props={"name": "bar"}),
        ],
        head_sha="sha-1",
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/a.ts",
                dst_label="Symbol",
                dst_key="/a.ts::foo#1",
            )
        ],
        head_sha="sha-1",
    )
    # Reingest foo at sha-2; bar is left behind at sha-1 (drifted).
    store.upsert_nodes(
        [GraphNode(label="Symbol", key="/a.ts::foo#1", props={"name": "foo"})],
        head_sha="sha-2",
    )
    drift = store.drift("sha-2")
    by_name = {d["name"]: d for d in drift}
    assert "bar" in by_name
    assert by_name["bar"]["status"] == "drifted"
    assert "foo" not in by_name
