"""Vacuum, ordinal stamping, and ``at_sha`` time-travel queries."""

from __future__ import annotations

import time
import uuid

import pytest

from code_memory.graph.falkor_store import FalkorStore, GraphEdge, GraphNode


def _falkor_available() -> bool:
    try:
        s = FalkorStore(graph_name="cm_vacuum_probe")
        s.graph.query("RETURN 1")
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _falkor_available(), reason="FalkorDB not reachable on configured host"
)


@pytest.fixture()
def store() -> FalkorStore:
    name = f"cm_vacuum_{uuid.uuid4().hex[:8]}"
    s = FalkorStore(graph_name=name)
    s.ensure_indexes()
    yield s
    s.graph.query("MATCH (n) DETACH DELETE n")


def test_upsert_stamps_ordinals(store: FalkorStore) -> None:
    store.upsert_nodes(
        [GraphNode(label="File", key="/a.ts", props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    rows = store.graph.query(
        "MATCH (f:File {key:'/a.ts'}) RETURN f.first_seen_ord, f.last_seen_ord"
    ).result_set
    assert rows == [[1, 1]]
    store.upsert_nodes(
        [GraphNode(label="File", key="/a.ts", props={})],
        head_sha="sha-2",
        head_ord=2,
    )
    rows = store.graph.query(
        "MATCH (f:File {key:'/a.ts'}) RETURN f.first_seen_ord, f.last_seen_ord"
    ).result_set
    assert rows == [[1, 2]]


def test_delete_stamps_invalid_ordinal_and_timestamp(store: FalkorStore) -> None:
    store.upsert_nodes(
        [GraphNode(label="File", key="/a.ts", props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    before = time.time()
    store.delete_file("/a.ts", head_sha="sha-2", head_ord=2)
    after = time.time()
    rows = store.graph.query(
        "MATCH (f:File {key:'/a.ts'}) RETURN f.invalid_sha, f.invalid_ord, f.invalid_at"
    ).result_set
    sha, ord_, ts = rows[0]
    assert sha == "sha-2"
    assert ord_ == 2
    # Falkor stores floats with reduced precision (~6 sig figs) so allow
    # a small tolerance on the bracketing wall-clock comparison.
    assert before - 1.0 <= ts <= after + 1.0


def test_vacuum_before_ord_drops_old_tombstones(store: FalkorStore) -> None:
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/old.ts", props={}),
            GraphNode(label="File", key="/recent.ts", props={}),
            GraphNode(label="File", key="/live.ts", props={}),
        ],
        head_sha="sha-1",
        head_ord=1,
    )
    store.delete_file("/old.ts", head_sha="sha-2", head_ord=2)
    store.delete_file("/recent.ts", head_sha="sha-10", head_ord=10)
    counts = store.vacuum(before_ord=5)
    assert counts["files"] == 1
    survivors = {
        r[0]
        for r in store.graph.query("MATCH (f:File) RETURN f.key").result_set
    }
    assert "/old.ts" not in survivors
    assert "/recent.ts" in survivors  # tombstoned but ord 10 > 5
    assert "/live.ts" in survivors


def test_vacuum_dry_run_reports_without_writing(store: FalkorStore) -> None:
    store.upsert_nodes(
        [GraphNode(label="File", key="/a.ts", props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    store.delete_file("/a.ts", head_sha="sha-2", head_ord=2)
    counts = store.vacuum(before_ord=5, dry_run=True)
    assert counts["files"] == 1
    still_there = store.graph.query(
        "MATCH (f:File {key:'/a.ts'}) RETURN count(f)"
    ).result_set[0][0]
    assert still_there == 1


def test_vacuum_older_than_uses_invalid_at(store: FalkorStore) -> None:
    store.upsert_nodes(
        [GraphNode(label="File", key="/a.ts", props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    store.delete_file("/a.ts", head_sha="sha-2", head_ord=2)
    # Backdate the invalid_at by 1 day so the older-than filter matches.
    store.graph.query(
        "MATCH (f:File {key:'/a.ts'}) SET f.invalid_at = $ts",
        {"ts": time.time() - 86400},
    )
    counts = store.vacuum(older_than_seconds=3600)
    assert counts["files"] == 1


def test_vacuum_all_drops_every_tombstone(store: FalkorStore) -> None:
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/a.ts", props={}),
            GraphNode(label="File", key="/b.ts", props={}),
        ],
        head_sha="sha-1",
        head_ord=1,
    )
    store.delete_file("/a.ts", head_sha="sha-2", head_ord=2)
    store.delete_file("/b.ts", head_sha="sha-9", head_ord=9)
    counts = store.vacuum(drop_all=True)
    assert counts["files"] == 2


def test_vacuum_requires_exactly_one_mode(store: FalkorStore) -> None:
    with pytest.raises(ValueError):
        store.vacuum()
    with pytest.raises(ValueError):
        store.vacuum(before_ord=5, drop_all=True)


def test_at_sha_returns_alive_nodes_only(store: FalkorStore) -> None:
    store.upsert_nodes(
        [GraphNode(label="Symbol", key="s::old#1", props={"name": "old"})],
        head_sha="sha-1",
        head_ord=1,
    )
    store.upsert_nodes(
        [GraphNode(label="Symbol", key="s::new#1", props={"name": "new"})],
        head_sha="sha-5",
        head_ord=5,
    )
    store.graph.query(
        """
        MATCH (s:Symbol {key:'s::old#1'})
        SET s.invalid_sha = 'sha-3', s.invalid_ord = 3
        """
    )
    # At ord=4: old was already tombstoned at ord=3, new not born yet.
    rows = store.at_sha("sha-4", 4)
    keys = {r["key"] for r in rows}
    assert "s::old#1" not in keys
    assert "s::new#1" not in keys
    # At ord=2: old still alive, new not yet.
    rows = store.at_sha("sha-2", 2)
    keys = {r["key"] for r in rows}
    assert "s::old#1" in keys
    assert "s::new#1" not in keys
    # At ord=6: only new is alive.
    rows = store.at_sha("sha-6", 6)
    keys = {r["key"] for r in rows}
    assert "s::old#1" not in keys
    assert "s::new#1" in keys


def test_callers_at_sha_recovers_pre_deletion_callers(store: FalkorStore) -> None:
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/caller.ts", props={}),
            GraphNode(
                label="Symbol",
                key="/target.ts::doIt#1",
                props={"name": "doIt", "file": "/target.ts", "start": 1, "end": 5, "kind": "function"},
            ),
        ],
        head_sha="sha-1",
        head_ord=1,
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/caller.ts",
                dst_label="Symbol",
                dst_key="/target.ts::doIt#1",
            )
        ],
        head_sha="sha-1",
        head_ord=1,
    )
    # Tombstone everything at ord=5.
    store.graph.query(
        """
        MATCH (n) SET n.invalid_sha = 'sha-5', n.invalid_ord = 5
        """
    )
    store.graph.query(
        """
        MATCH ()-[r]->() SET r.invalid_sha = 'sha-5', r.invalid_ord = 5
        """
    )
    # Pre-deletion (ord=3): should see the call edge.
    rows = store.callers_at_sha("doIt", "sha-3", 3)
    assert any(r["caller"] == "/caller.ts" for r in rows)
    # Post-deletion (ord=7): nothing alive.
    rows = store.callers_at_sha("doIt", "sha-7", 7)
    assert rows == []
