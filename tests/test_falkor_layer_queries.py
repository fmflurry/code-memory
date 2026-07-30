"""Live-FalkorDB tests for the Layer-node query surface:

  - ``FalkorStore.sweep_stale(label, head_sha, head_ord=None) -> int``
  - ``FalkorStore.layer_violations() -> list[dict[str, Any]]``
  - ``FalkorStore.layer_of(file_key) -> dict[str, Any] | None``
  - ``Pipeline._index_architecture`` idempotency (needs real MERGE
    semantics, so it's exercised here rather than with a fake graph).

None of these exist yet — RED until implemented. Follows the live-Falkor
skip-guard convention established in test_graph_vacuum_at_sha.py: skip
the whole module when FalkorDB isn't reachable rather than mocking
Cypher execution, because these queries' correctness depends on real
multi-hop pattern matching (DEFINES joins, rank comparison) that a fake
recording graph can't verify.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from code_memory.graph.falkor_store import FalkorStore, GraphEdge, GraphNode


def _falkor_available() -> bool:
    try:
        s = FalkorStore(graph_name="cm_layer_probe")
        s.graph.query("RETURN 1")
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _falkor_available(), reason="FalkorDB not reachable on configured host"
)


@pytest.fixture()
def store() -> FalkorStore:
    name = f"cm_layer_{uuid.uuid4().hex[:8]}"
    s = FalkorStore(graph_name=name)
    s.ensure_indexes()
    yield s
    s.graph.query("MATCH (n) DETACH DELETE n")


# --------------------------------------------------------------- sweep_stale


def test_sweep_stale_tombstones_node_not_reconfirmed_at_head(store: FalkorStore) -> None:
    store.upsert_nodes(
        [
            GraphNode(label="Layer", key="l-stale", props={"name": "domain", "rank": 0}),
            GraphNode(label="Layer", key="l-current", props={"name": "application", "rank": 1}),
        ],
        head_sha="sha-1",
        head_ord=1,
    )
    # Only l-current gets re-seen at sha-2; l-stale's last_seen_sha stays sha-1.
    store.upsert_nodes(
        [GraphNode(label="Layer", key="l-current", props={"name": "application", "rank": 1})],
        head_sha="sha-2",
        head_ord=2,
    )

    swept = store.sweep_stale("Layer", head_sha="sha-2", head_ord=2)

    assert swept == 1
    stale_row = store.graph.query(
        "MATCH (l:Layer {key:'l-stale'}) RETURN l.invalid_sha, l.invalid_ord"
    ).result_set[0]
    assert stale_row == ["sha-2", 2]


def test_sweep_stale_leaves_current_node_untouched(store: FalkorStore) -> None:
    store.upsert_nodes(
        [GraphNode(label="Layer", key="l-current", props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    store.upsert_nodes(
        [GraphNode(label="Layer", key="l-current", props={})],
        head_sha="sha-2",
        head_ord=2,
    )

    store.sweep_stale("Layer", head_sha="sha-2", head_ord=2)

    row = store.graph.query(
        "MATCH (l:Layer {key:'l-current'}) RETURN l.invalid_sha"
    ).result_set[0]
    assert row == [None]


def test_sweep_stale_does_not_restamp_already_tombstoned_node(store: FalkorStore) -> None:
    store.upsert_nodes(
        [GraphNode(label="Layer", key="l-gone", props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    store.graph.query(
        "MATCH (l:Layer {key:'l-gone'}) "
        "SET l.invalid_sha = 'sha-2', l.invalid_ord = 2, l.invalid_at = 123.0"
    )

    swept = store.sweep_stale("Layer", head_sha="sha-9", head_ord=9)

    assert swept == 0
    row = store.graph.query(
        "MATCH (l:Layer {key:'l-gone'}) "
        "RETURN l.invalid_sha, l.invalid_ord, l.invalid_at"
    ).result_set[0]
    assert row == ["sha-2", 2, 123.0]


def test_sweep_stale_rejects_unknown_label(store: FalkorStore) -> None:
    """Defect 6 (MEDIUM): ``sweep_stale`` f-string-interpolates ``label``
    straight into the Cypher ``MATCH (n:{label})`` clause with no
    allowlist — the first public method to take a raw label string
    without validation. Not exploitable today (no caller passes an
    untrusted label), but pin the fix: an unexpected label must raise,
    not silently run. Intended fix: validate against a fixed set
    ``{"File", "Symbol", "Module", "Layer"}`` before building the query."""
    with pytest.raises(ValueError):
        store.sweep_stale("Bogus", head_sha="sha-1", head_ord=1)


def test_sweep_stale_rejects_cypher_injection_payload_in_label(
    store: FalkorStore,
) -> None:
    """Same fix, adversarial input: a label string crafted to break out
    of the ``MATCH (n:{label})`` clause must be rejected before any
    query executes — proven by a seeded node surviving the call."""
    store.upsert_nodes(
        [GraphNode(label="Layer", key="l-safe", props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    malicious = "Layer) DETACH DELETE n //"

    with pytest.raises(ValueError):
        store.sweep_stale(malicious, head_sha="sha-2", head_ord=2)

    row = store.graph.query(
        "MATCH (l:Layer {key:'l-safe'}) RETURN count(l)"
    ).result_set[0][0]
    assert row == 1, "the injection payload must never have reached the DB"


def test_sweep_stale_result_matches_vacuum_eligibility_predicate(store: FalkorStore) -> None:
    """``vacuum`` only special-cases File/Symbol today, but its tombstone
    predicate (invalid_sha IS NOT NULL AND invalid_ord IS NOT NULL AND
    invalid_ord <= before_ord) is label-agnostic. This proves sweep_stale
    produces data that predicate would already collect."""
    store.upsert_nodes(
        [GraphNode(label="Layer", key="l-orphan", props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    store.sweep_stale("Layer", head_sha="sha-9", head_ord=9)

    eligible = store.graph.query(
        "MATCH (n:Layer {key:'l-orphan'}) "
        "WHERE n.invalid_sha IS NOT NULL AND n.invalid_ord IS NOT NULL "
        "AND n.invalid_ord <= 10 RETURN count(n)"
    ).result_set[0][0]
    assert eligible == 1


# ------------------------------------------------------------- layer_of


def test_layer_of_returns_layer_for_classified_file(store: FalkorStore) -> None:
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/domain/invoice.ts", props={}),
            GraphNode(label="Layer", key="l1", props={"name": "domain", "rank": 0}),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/domain/invoice.ts",
                dst_label="Layer",
                dst_key="l1",
            )
        ]
    )

    result = store.layer_of("/repo/domain/invoice.ts")

    assert result == {"name": "domain", "rank": 0}


def test_layer_of_returns_none_for_unclassified_file(store: FalkorStore) -> None:
    store.upsert_nodes([GraphNode(label="File", key="/repo/loose.ts", props={})])

    assert store.layer_of("/repo/loose.ts") is None


def test_layer_of_returns_none_for_tombstoned_file(store: FalkorStore) -> None:
    """Defect 3 (HIGH): every other read query in falkor_store.py filters
    ``invalid_sha IS NULL`` (see ``_callers_one_hop``, ``_defines_at``,
    ``layer_violations``) — ``layer_of`` doesn't, so a tombstoned (soft-
    deleted) file still reports a layer."""
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/domain/gone.ts", props={}),
            GraphNode(label="Layer", key="l1", props={"name": "domain", "rank": 0}),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/domain/gone.ts",
                dst_label="Layer",
                dst_key="l1",
            )
        ]
    )
    store.graph.query(
        "MATCH (f:File {key:'/repo/domain/gone.ts'}) SET f.invalid_sha = 'gone'"
    )

    assert store.layer_of("/repo/domain/gone.ts") is None


def test_layer_of_returns_none_when_layer_node_itself_is_tombstoned(
    store: FalkorStore,
) -> None:
    """Same fix, the other endpoint: if the Layer node was swept (e.g. by
    Defect 2's ``sweep_stale`` fix) while a stale IN_LAYER edge still
    points at it, ``layer_of`` must not report a dead layer."""
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/domain/live.ts", props={}),
            GraphNode(label="Layer", key="l1", props={"name": "domain", "rank": 0}),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/domain/live.ts",
                dst_label="Layer",
                dst_key="l1",
            )
        ]
    )
    store.graph.query("MATCH (l:Layer {key:'l1'}) SET l.invalid_sha = 'gone'")

    assert store.layer_of("/repo/domain/live.ts") is None


def test_layer_of_ignores_a_tombstoned_in_layer_edge(store: FalkorStore) -> None:
    """HIGH 2 (round 4): ``layer_of`` filters ``f.invalid_sha`` and
    ``l.invalid_sha`` but uses an anonymous ``-[:IN_LAYER]->`` pattern —
    no variable bound to the EDGE, so a tombstoned edge between two
    otherwise-live nodes (exactly what HIGH 1's classified ->
    unclassified fix produces) is invisible to the ``WHERE`` clause and
    still gets traversed. Distinct from
    ``test_layer_of_returns_none_when_layer_node_itself_is_tombstoned``
    above — here the FILE and the LAYER are both live; only the EDGE
    between them is tombstoned."""
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/domain/x.ts", props={}),
            GraphNode(label="Layer", key="l1", props={"name": "domain", "rank": 0}),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/domain/x.ts",
                dst_label="Layer",
                dst_key="l1",
            )
        ]
    )
    store.graph.query(
        "MATCH (f:File {key:'/repo/domain/x.ts'})-[r:IN_LAYER]->(l:Layer {key:'l1'}) "
        "SET r.invalid_sha = 'gone'"
    )

    assert store.layer_of("/repo/domain/x.ts") is None


# ------------------------------------------------------- layer_violations


def _seed_layered_files(store: FalkorStore) -> None:
    """Two-layer fixture: a domain file CALLS a symbol defined in an
    infrastructure file — the canonical inward-dependency violation."""
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/domain/UseCase.cs", props={}),
            GraphNode(label="File", key="/repo/infrastructure/Impl.cs", props={}),
            GraphNode(label="Layer", key="l-domain", props={"name": "domain", "rank": 0}),
            GraphNode(
                label="Layer", key="l-infra", props={"name": "infrastructure", "rank": 2}
            ),
            GraphNode(
                label="Symbol",
                key="/repo/infrastructure/Impl.cs::DoWork#1",
                props={"name": "DoWork"},
            ),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/domain/UseCase.cs",
                dst_label="Layer",
                dst_key="l-domain",
            ),
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/infrastructure/Impl.cs",
                dst_label="Layer",
                dst_key="l-infra",
            ),
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/repo/infrastructure/Impl.cs",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/Impl.cs::DoWork#1",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/repo/domain/UseCase.cs",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/Impl.cs::DoWork#1",
            ),
        ]
    )


def test_layer_violations_empty_on_clean_graph(store: FalkorStore) -> None:
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/domain/A.cs", props={}),
            GraphNode(label="Layer", key="l-domain", props={"name": "domain", "rank": 0}),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/domain/A.cs",
                dst_label="Layer",
                dst_key="l-domain",
            )
        ]
    )

    assert store.layer_violations() == []


def test_layer_violations_flags_domain_depending_on_infrastructure(
    store: FalkorStore,
) -> None:
    _seed_layered_files(store)

    violations = store.layer_violations()

    assert len(violations) == 1
    v = violations[0]
    assert v["file"] == "/repo/domain/UseCase.cs"
    assert v["layer"] == "domain"
    assert v["depends_on_file"] == "/repo/infrastructure/Impl.cs"
    assert v["depends_on_layer"] == "infrastructure"
    assert v["symbol"] == "DoWork"


def test_layer_violations_excludes_tombstoned_dependent_file(store: FalkorStore) -> None:
    _seed_layered_files(store)
    store.graph.query(
        "MATCH (f:File {key:'/repo/domain/UseCase.cs'}) SET f.invalid_sha = 'gone'"
    )

    assert store.layer_violations() == []


def test_layer_violations_excludes_tombstoned_target_file(store: FalkorStore) -> None:
    _seed_layered_files(store)
    store.graph.query(
        "MATCH (f:File {key:'/repo/infrastructure/Impl.cs'}) SET f.invalid_sha = 'gone'"
    )

    assert store.layer_violations() == []


def test_layer_violations_same_layer_dependency_is_not_a_violation(
    store: FalkorStore,
) -> None:
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/domain/A.cs", props={}),
            GraphNode(label="File", key="/repo/domain/B.cs", props={}),
            GraphNode(label="Layer", key="l-domain", props={"name": "domain", "rank": 0}),
            GraphNode(
                label="Symbol", key="/repo/domain/B.cs::Helper#1", props={"name": "Helper"}
            ),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/domain/A.cs",
                dst_label="Layer",
                dst_key="l-domain",
            ),
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/domain/B.cs",
                dst_label="Layer",
                dst_key="l-domain",
            ),
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/repo/domain/B.cs",
                dst_label="Symbol",
                dst_key="/repo/domain/B.cs::Helper#1",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/repo/domain/A.cs",
                dst_label="Symbol",
                dst_key="/repo/domain/B.cs::Helper#1",
            ),
        ]
    )

    assert store.layer_violations() == []


def test_layer_violations_allowed_outward_to_inward_direction_is_not_flagged(
    store: FalkorStore,
) -> None:
    """Infrastructure (rank 2) calling into Domain (rank 0) is the
    ALLOWED clean-architecture direction — must not be reported."""
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/infrastructure/Repo.cs", props={}),
            GraphNode(label="File", key="/repo/domain/Entity.cs", props={}),
            GraphNode(
                label="Layer", key="l-infra", props={"name": "infrastructure", "rank": 2}
            ),
            GraphNode(label="Layer", key="l-domain", props={"name": "domain", "rank": 0}),
            GraphNode(
                label="Symbol", key="/repo/domain/Entity.cs::Ctor#1", props={"name": "Entity"}
            ),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/infrastructure/Repo.cs",
                dst_label="Layer",
                dst_key="l-infra",
            ),
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/domain/Entity.cs",
                dst_label="Layer",
                dst_key="l-domain",
            ),
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/repo/domain/Entity.cs",
                dst_label="Symbol",
                dst_key="/repo/domain/Entity.cs::Ctor#1",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/repo/infrastructure/Repo.cs",
                dst_label="Symbol",
                dst_key="/repo/domain/Entity.cs::Ctor#1",
            ),
        ]
    )

    assert store.layer_violations() == []


def test_layer_violations_never_returns_self_violation_row(store: FalkorStore) -> None:
    """Defect 4 (HIGH) invariant, independent of any reclassification
    flow: ``layer_violations()`` must never emit a row where the
    dependent file and the dependency-target file are the SAME file.
    The Cypher pattern binds ``lo`` and ``hi`` from two separate
    ``IN_LAYER`` matches with no ``lo <> hi`` guard, so nothing stops
    both from binding to one File node that (a) has two live IN_LAYER
    edges to layers of different rank — exactly what Defect 4's
    "stale edge never removed on reclassification" bug produces — and
    (b) calls a symbol it defines itself (a plain recursive method,
    which is common, ordinary code). That combination currently
    produces a spurious self-violation row."""
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/self.ts", props={}),
            GraphNode(label="Layer", key="l-domain", props={"name": "domain", "rank": 0}),
            GraphNode(
                label="Layer", key="l-infra", props={"name": "infrastructure", "rank": 2}
            ),
            GraphNode(
                label="Symbol",
                key="/repo/self.ts::Recurse#0",
                props={"name": "Recurse"},
            ),
        ]
    )
    store.upsert_edges(
        [
            # Simulates the un-swept stale edge Defect 4 leaves behind:
            # the SAME file with two live IN_LAYER edges to two layers
            # of different rank.
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/self.ts",
                dst_label="Layer",
                dst_key="l-domain",
            ),
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/self.ts",
                dst_label="Layer",
                dst_key="l-infra",
            ),
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/repo/self.ts",
                dst_label="Symbol",
                dst_key="/repo/self.ts::Recurse#0",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/repo/self.ts",
                dst_label="Symbol",
                dst_key="/repo/self.ts::Recurse#0",
            ),
        ]
    )

    violations = store.layer_violations()

    assert all(v["file"] != v["depends_on_file"] for v in violations)


def test_layer_violations_excludes_tombstoned_in_layer_edge(store: FalkorStore) -> None:
    """HIGH 2 (round 4): ``layer_violations()`` uses anonymous
    ``(lo:File)-[:IN_LAYER]->(loLayer:Layer)`` / ``(hi:File)-[:IN_LAYER]->
    (hiLayer:Layer)`` patterns — no variable bound to either edge, so no
    ``WHERE`` clause can exclude a tombstoned one. Both endpoint FILEs
    here are live; only the dependent file's ``IN_LAYER`` edge is
    tombstoned (exactly what a correct HIGH-1 fix produces once a file
    reclassifies) — the violation must disappear, not persist via the
    stale edge."""
    _seed_layered_files(store)
    store.graph.query(
        "MATCH (f:File {key:'/repo/domain/UseCase.cs'})-[r:IN_LAYER]->(l:Layer {key:'l-domain'}) "
        "SET r.invalid_sha = 'gone'"
    )

    assert store.layer_violations() == []


def test_layer_violations_excludes_tombstoned_layer_node_on_either_end(
    store: FalkorStore,
) -> None:
    """HIGH 2 (round 4): ``layer_violations()`` never checks
    ``loLayer.invalid_sha``/``hiLayer.invalid_sha`` at all — a Layer node
    swept by ``sweep_stale`` (e.g. because its directory was renamed)
    must not keep producing violations through it, on EITHER the
    dependent or the target side."""
    _seed_layered_files(store)
    store.graph.query("MATCH (l:Layer {key:'l-domain'}) SET l.invalid_sha = 'gone'")

    assert store.layer_violations() == []


def test_layer_violations_excludes_tombstoned_target_layer_node(
    store: FalkorStore,
) -> None:
    _seed_layered_files(store)
    store.graph.query("MATCH (l:Layer {key:'l-infra'}) SET l.invalid_sha = 'gone'")

    assert store.layer_violations() == []


def test_layer_violations_stops_reporting_after_dependent_file_moved_out_of_the_violating_layer(
    store: FalkorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH 2 (round 4) end-to-end scenario from the review: FileA
    (domain) calls into FileB (infrastructure) -> correctly flagged.
    Team "fixes" it by moving FileA into infrastructure too (infra ->
    infra, allowed). The old domain IN_LAYER edge is correctly
    tombstoned by the (already-fixed) reclassification path and a live
    infrastructure edge added — but without HIGH 2's edge/Layer
    invalid_sha filters, ``layer_violations()`` still traverses the
    stale tombstoned domain edge and keeps reporting the already-fixed
    violation indefinitely. Reproduced empirically against the deployed
    code before writing this (see report)."""
    from code_memory.orchestrator import pipeline as pipeline_mod
    from code_memory.orchestrator.pipeline import IngestStats, Pipeline

    file_a = "/repo/src/FileA.cs"
    file_b = "/repo/src/FileB.cs"
    store.upsert_nodes(
        [
            GraphNode(label="File", key=file_a, props={}),
            GraphNode(label="File", key=file_b, props={}),
            GraphNode(
                label="Symbol", key=f"{file_b}::DoWork#1", props={"name": "DoWork"}
            ),
        ],
        head_sha="sha-1",
        head_ord=1,
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key=file_b,
                dst_label="Symbol",
                dst_key=f"{file_b}::DoWork#1",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key=file_a,
                dst_label="Symbol",
                dst_key=f"{file_b}::DoWork#1",
            ),
        ]
    )

    pipe = Pipeline.__new__(Pipeline)
    pipe.graph = store  # type: ignore[assignment]

    def classify_run1(rel_path: str, *a: object, **kw: object) -> tuple:
        if str(rel_path) == "src/FileA.cs":
            return ("mod", "domain", 0)
        return ("mod", "infrastructure", 2)

    monkeypatch.setattr(pipeline_mod, "classify_file_layer", classify_run1)
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-1", head_ord=1)

    violations = store.layer_violations()
    assert len(violations) == 1, f"sanity check: expected the violation, got {violations}"
    assert violations[0]["file"] == file_a

    monkeypatch.setattr(
        pipeline_mod, "classify_file_layer", lambda *a, **kw: ("mod", "infrastructure", 2)
    )
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-2", head_ord=2)

    assert store.layer_violations() == [], (
        "the already-fixed violation must not keep being reported via "
        "the stale (correctly tombstoned) domain IN_LAYER edge"
    )


# ------------------------------- Defect 4 (HIGH): stale IN_LAYER edge on
# reclassification
#
# ``upsert_edges`` only MERGEs the (src, type, dst) triple it's given —
# it never removes a file's PRIOR ``IN_LAYER`` edge to a DIFFERENT Layer.
# So after a file's classification changes across two ``_index_architecture``
# runs (e.g. a dir rename, or — as here — the same path classifying
# differently across runs), one File ends up with TWO live IN_LAYER
# edges. Fix: before writing the new edge, ``_index_architecture`` must
# remove/tombstone any of the file's existing IN_LAYER edges whose
# destination differs from the newly computed Layer key.
#
# ``classify_file_layer`` is monkeypatched between the two runs to
# deterministically simulate "this file's classification changed" —
# the SAME technique the review used to describe the bug (a dir
# rename), without needing an actual second filesystem layout.


def test_reclassification_leaves_exactly_one_live_in_layer_edge(
    store: FalkorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.orchestrator import pipeline as pipeline_mod
    from code_memory.orchestrator.pipeline import IngestStats, Pipeline

    file_key = "/repo/src/thing.ts"
    store.upsert_nodes(
        [GraphNode(label="File", key=file_key, props={})],
        head_sha="sha-1",
        head_ord=1,
    )

    pipe = Pipeline.__new__(Pipeline)
    pipe.graph = store  # type: ignore[assignment]

    monkeypatch.setattr(
        pipeline_mod, "classify_file_layer", lambda *a, **kw: ("mod", "domain", 0)
    )
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-1", head_ord=1)

    monkeypatch.setattr(
        pipeline_mod,
        "classify_file_layer",
        lambda *a, **kw: ("mod", "infrastructure", 2),
    )
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-2", head_ord=2)

    live_edges = store.graph.query(
        "MATCH (f:File {key:$key})-[r:IN_LAYER]->(l:Layer) "
        "WHERE r.invalid_sha IS NULL "
        "RETURN l.name",
        {"key": file_key},
    ).result_set
    assert len(live_edges) == 1, (
        f"expected exactly one live IN_LAYER edge after reclassification, got {live_edges}"
    )
    assert live_edges[0][0] == "infrastructure"

    # layer_of must deterministically report the NEW layer, not be at the
    # mercy of an unordered ``LIMIT 1`` over two live edges.
    assert store.layer_of(file_key) == {"name": "infrastructure", "rank": 2}


def test_reclassification_to_unclassified_tombstones_the_stale_in_layer_edge(
    store: FalkorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH 1 (round 4): the edge-invalidation query
    (``MATCH (f:File {key:$key})-[r:IN_LAYER]->(l:Layer) WHERE l.key <>
    $new_layer_key ...``) only ran inside the ``classified is not None``
    branch — a file going classified -> unclassified (e.g. refactored
    out of any layer directory) hit ``if classified is None: ...
    continue`` FIRST, so its old ``IN_LAYER`` edge stayed live forever.
    ``sweep_stale`` only tombstones NODES, never edges, so nothing else
    cleans it up.

    Reproduced empirically against the deployed code before writing this
    (see report): the stale edge survives, ``layer_of()`` wrongly keeps
    reporting the old layer even though ``f.context`` is correctly NULL
    — a verifiable prop/edge disagreement.

    Companion ``other_key`` keeps classifying into the SAME layer across
    both runs so the Layer node stays alive throughout — isolating the
    EDGE-lifecycle bug (this test) from Layer-NODE sweeping (already
    covered elsewhere) and letting this test assert the Layer node is
    correctly NOT tombstoned as a result."""
    from code_memory.orchestrator import pipeline as pipeline_mod
    from code_memory.orchestrator.pipeline import IngestStats, Pipeline

    file_key = "/repo/src/BcA/Domain/Foo.cs"
    other_key = "/repo/src/BcA/Domain/Other.cs"
    store.upsert_nodes(
        [
            GraphNode(label="File", key=file_key, props={}),
            GraphNode(label="File", key=other_key, props={}),
        ],
        head_sha="sha-1",
        head_ord=1,
    )
    pipe = Pipeline.__new__(Pipeline)
    pipe.graph = store  # type: ignore[assignment]

    monkeypatch.setattr(
        pipeline_mod, "classify_file_layer", lambda *a, **kw: ("BcA", "domain", 0)
    )
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-1", head_ord=1)

    layer_key = "/repo::layer::BcA::domain"
    live_edges = store.graph.query(
        "MATCH (f:File {key:$key})-[r:IN_LAYER]->(l:Layer) "
        "WHERE r.invalid_sha IS NULL RETURN l.key",
        {"key": file_key},
    ).result_set
    assert live_edges == [[layer_key]], "sanity check: classified at sha-1"

    def classify_run2(rel_path: str, *a: object, **kw: object) -> tuple | None:
        if str(rel_path) == "src/BcA/Domain/Foo.cs":
            return None  # refactored out of any layer directory
        return ("BcA", "domain", 0)  # other_key keeps the Layer node alive

    monkeypatch.setattr(pipeline_mod, "classify_file_layer", classify_run2)
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-2", head_ord=2)

    live_edges = store.graph.query(
        "MATCH (f:File {key:$key})-[r:IN_LAYER]->(l:Layer) "
        "WHERE r.invalid_sha IS NULL RETURN l.key",
        {"key": file_key},
    ).result_set
    assert live_edges == [], (
        f"expected zero live IN_LAYER edges after going unclassified, got {live_edges}"
    )

    assert store.layer_of(file_key) is None

    context_row = store.graph.query(
        "MATCH (f:File {key:$key}) RETURN f.context", {"key": file_key}
    ).result_set[0]
    assert context_row == [None]

    tombstone_row = store.graph.query(
        "MATCH (f:File {key:$key})-[r:IN_LAYER]->(l:Layer {key:$layer_key}) "
        "RETURN r.invalid_sha, r.invalid_ord, r.invalid_at",
        {"key": file_key, "layer_key": layer_key},
    ).result_set[0]
    assert tombstone_row[0] == "sha-2"
    assert tombstone_row[1] == 2
    assert tombstone_row[2] is not None

    # The Layer node itself must NOT be tombstoned — other_key still
    # confirms it every run.
    layer_row = store.graph.query(
        "MATCH (l:Layer {key:$key}) RETURN l.invalid_sha", {"key": layer_key}
    ).result_set[0]
    assert layer_row == [None]


# --------------------------------------------- FIX 1: File.context prop
# lifecycle across reclassification
#
# These need real MERGE/SET semantics (the fake recording graph can't
# verify a property actually gets overwritten or cleared), hence the
# live-Falkor fixture here rather than in test_pipeline_architecture.py.


def test_reclassification_updates_file_context_prop_not_just_the_edge(
    store: FalkorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.orchestrator import pipeline as pipeline_mod
    from code_memory.orchestrator.pipeline import IngestStats, Pipeline

    file_key = "/repo/src/thing.ts"
    store.upsert_nodes(
        [GraphNode(label="File", key=file_key, props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    pipe = Pipeline.__new__(Pipeline)
    pipe.graph = store  # type: ignore[assignment]

    monkeypatch.setattr(
        pipeline_mod, "classify_file_layer", lambda *a, **kw: ("bc-a", "domain", 0)
    )
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-1", head_ord=1)

    row = store.graph.query(
        "MATCH (f:File {key:$key}) RETURN f.context", {"key": file_key}
    ).result_set[0]
    assert row == ["bc-a"], "sanity check: context stamped on first run"

    monkeypatch.setattr(
        pipeline_mod, "classify_file_layer", lambda *a, **kw: ("bc-b", "infrastructure", 2)
    )
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-2", head_ord=2)

    row = store.graph.query(
        "MATCH (f:File {key:$key}) RETURN f.context", {"key": file_key}
    ).result_set[0]
    assert row == ["bc-b"], "File.context must follow reclassification, not just the edge"


def test_file_that_stops_classifying_has_no_stale_context_left_over(
    store: FalkorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.orchestrator import pipeline as pipeline_mod
    from code_memory.orchestrator.pipeline import IngestStats, Pipeline

    file_key = "/repo/src/thing.ts"
    store.upsert_nodes(
        [GraphNode(label="File", key=file_key, props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    pipe = Pipeline.__new__(Pipeline)
    pipe.graph = store  # type: ignore[assignment]

    monkeypatch.setattr(
        pipeline_mod, "classify_file_layer", lambda *a, **kw: ("bc-a", "domain", 0)
    )
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-1", head_ord=1)
    row = store.graph.query(
        "MATCH (f:File {key:$key}) RETURN f.context", {"key": file_key}
    ).result_set[0]
    assert row == ["bc-a"], "sanity check: context stamped on first run"

    monkeypatch.setattr(pipeline_mod, "classify_file_layer", lambda *a, **kw: None)
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-2", head_ord=2)

    row = store.graph.query(
        "MATCH (f:File {key:$key}) RETURN f.context", {"key": file_key}
    ).result_set[0]
    assert row == [None], (
        "a file that no longer classifies into any layer must not keep "
        "a stale context prop from a previous ingest"
    )


def test_untouched_classified_file_last_seen_stamp_survives_a_later_ingest(
    store: FalkorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH (round 3): ``_index_architecture`` re-derives classification
    for ALL live files every run. Routing the File-context write through
    ``GraphNode``/``upsert_nodes`` means a file NEVER reparsed this
    ingest (no ``_upsert_graph`` call for it) still gets its
    ``last_seen_sha``/``last_seen_ord`` bumped to the new head purely
    because ``_index_architecture`` re-touched it — corrupting the
    "last_seen = last ingest that actually saw this node" invariant, and
    since ``File`` is sweepable, a genuinely missed deletion would never
    surface via ``sweep_stale``/``drift``.

    Simulates: file classified (as if by ``_upsert_graph``) at sha-1.
    A LATER delta ingest at sha-2 does NOT touch this file at all (no
    ``_upsert_graph`` call — the file's content didn't change and it
    isn't in this delta), but ``_index_architecture`` still runs at
    sha-2 and re-derives the SAME classification for it. Its
    last_seen_sha/ord must still read sha-1/1 afterwards, not sha-2/2 —
    while its ``context`` (already correct) and ``lang`` prop stay
    intact throughout."""
    from code_memory.orchestrator import pipeline as pipeline_mod
    from code_memory.orchestrator.pipeline import IngestStats, Pipeline

    file_key = "/repo/src/thing.ts"
    store.upsert_nodes(
        [GraphNode(label="File", key=file_key, props={"lang": "ts"})],
        head_sha="sha-1",
        head_ord=1,
    )
    pipe = Pipeline.__new__(Pipeline)
    pipe.graph = store  # type: ignore[assignment]

    monkeypatch.setattr(
        pipeline_mod, "classify_file_layer", lambda *a, **kw: ("bc-a", "domain", 0)
    )
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-1", head_ord=1)

    row = store.graph.query(
        "MATCH (f:File {key:$key}) RETURN f.last_seen_sha, f.last_seen_ord, "
        "f.context, f.lang",
        {"key": file_key},
    ).result_set[0]
    assert row == ["sha-1", 1, "bc-a", "ts"], "sanity check after first run"

    # Second ingest at sha-2: file_key is NOT part of this delta at all
    # (no _upsert_graph call for it), but _index_architecture still
    # unconditionally re-derives its (unchanged) classification.
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-2", head_ord=2)

    row = store.graph.query(
        "MATCH (f:File {key:$key}) RETURN f.last_seen_sha, f.last_seen_ord, "
        "f.context, f.lang",
        {"key": file_key},
    ).result_set[0]
    assert row == ["sha-1", 1, "bc-a", "ts"], (
        "an untouched-but-classified file's last_seen_sha/last_seen_ord "
        "must stay at the ingest that actually saw it, not the "
        "_index_architecture run that merely re-derived its unchanged "
        "classification; context and lang must remain correct throughout"
    )


# --------------------------------------- _index_architecture idempotency


def test_index_architecture_run_twice_produces_no_duplicates(store: FalkorStore) -> None:
    """True idempotency needs real MERGE semantics — a fake recording
    graph can't verify this, hence the live-Falkor fixture here rather
    than in test_pipeline_architecture.py."""
    from code_memory.orchestrator.pipeline import IngestStats, Pipeline

    store.upsert_nodes(
        [GraphNode(label="File", key="/repo/src/app/domain/invoice.ts", props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    pipe = Pipeline.__new__(Pipeline)
    pipe.graph = store  # type: ignore[assignment]

    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-1", head_ord=1)
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-1", head_ord=1)

    layer_count = store.graph.query("MATCH (l:Layer) RETURN count(l)").result_set[0][0]
    edge_count = store.graph.query(
        "MATCH ()-[r:IN_LAYER]->() RETURN count(r)"
    ).result_set[0][0]
    assert layer_count == 1
    assert edge_count == 1


# --------------------------------- Defect 2 (HIGH): orphaned Layer nodes
# actually get swept across real ingests
#
# These exercise ``Pipeline._index_architecture`` against a real
# FalkorStore across TWO runs — proving the observable outcome
# (``invalid_sha`` actually gets set, and the vacuum predicate actually
# picks the node up) rather than just that ``sweep_stale`` was called
# with some args (that wiring-level assertion lives in
# test_pipeline_architecture.py against the fake graph).


def test_layer_node_tombstoned_when_its_directory_is_renamed_across_ingests(
    store: FalkorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename ``infrastructure/`` -> ``domain/`` (simulated the same way
    as the reclassification tests above: monkeypatching
    ``classify_file_layer`` between two ``_index_architecture`` runs at
    different ``head_sha``). The OLD Layer node ("infrastructure") is no
    longer reconfirmed by any file and must end up tombstoned."""
    from code_memory.orchestrator import pipeline as pipeline_mod
    from code_memory.orchestrator.pipeline import IngestStats, Pipeline

    file_key = "/repo/src/thing.ts"
    store.upsert_nodes(
        [GraphNode(label="File", key=file_key, props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    pipe = Pipeline.__new__(Pipeline)
    pipe.graph = store  # type: ignore[assignment]

    monkeypatch.setattr(
        pipeline_mod,
        "classify_file_layer",
        lambda *a, **kw: ("mod", "infrastructure", 2),
    )
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-1", head_ord=1)

    old_layer_key = "/repo::layer::mod::infrastructure"
    before = store.graph.query(
        "MATCH (l:Layer {key:$key}) RETURN l.invalid_sha", {"key": old_layer_key}
    ).result_set[0]
    assert before == [None], "sanity check: old Layer node starts live"

    monkeypatch.setattr(
        pipeline_mod, "classify_file_layer", lambda *a, **kw: ("mod", "domain", 0)
    )
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-2", head_ord=2)

    after = store.graph.query(
        "MATCH (l:Layer {key:$key}) RETURN l.invalid_sha, l.invalid_ord",
        {"key": old_layer_key},
    ).result_set[0]
    assert after == ["sha-2", 2], (
        "expected the orphaned 'infrastructure' Layer node to be "
        "tombstoned by sweep_stale on the run that stopped reconfirming it"
    )


def test_layer_node_tombstoned_and_vacuum_eligible_when_its_last_file_is_deleted(
    store: FalkorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the last file backing a Layer must eventually free that
    Layer node for vacuum. ``delete_file`` tombstones the File (and its
    edges) but never the Layer — closing that gap requires BOTH (a) the
    Defect-2 sweep_stale wiring, AND (b) ``_index_architecture``'s File
    query excluding already-tombstoned files (``WHERE f.invalid_sha IS
    NULL``) — otherwise the tombstoned File would still get reclassified
    and re-confirm (un-tombstone) the very Layer node this test expects
    to go stale. Both changes are necessary for this scenario; call this
    out explicitly to the implementer rather than leaving it implicit."""
    from code_memory.orchestrator import pipeline as pipeline_mod
    from code_memory.orchestrator.pipeline import IngestStats, Pipeline

    file_key = "/repo/src/only.ts"
    store.upsert_nodes(
        [GraphNode(label="File", key=file_key, props={})],
        head_sha="sha-1",
        head_ord=1,
    )
    pipe = Pipeline.__new__(Pipeline)
    pipe.graph = store  # type: ignore[assignment]

    monkeypatch.setattr(
        pipeline_mod, "classify_file_layer", lambda *a, **kw: ("mod", "domain", 0)
    )
    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-1", head_ord=1)

    layer_key = "/repo::layer::mod::domain"

    store.delete_file(file_key, head_sha="sha-2", head_ord=2)

    pipe._index_architecture(Path("/repo"), IngestStats(), head_sha="sha-2", head_ord=2)

    row = store.graph.query(
        "MATCH (l:Layer {key:$key}) RETURN l.invalid_sha, l.invalid_ord",
        {"key": layer_key},
    ).result_set[0]
    assert row == ["sha-2", 2], (
        "expected the Layer node to be tombstoned once its last file was "
        "deleted and no longer reconfirmed it"
    )

    eligible = store.graph.query(
        "MATCH (l:Layer {key:$key}) WHERE l.invalid_sha IS NOT NULL "
        "AND l.invalid_ord IS NOT NULL AND l.invalid_ord <= 10 "
        "RETURN count(l)",
        {"key": layer_key},
    ).result_set[0][0]
    assert eligible == 1, "vacuum's before_ord predicate must now pick up this node"


# ------------------------------------------- test/spec-file exclusion from
# layer_violations()
#
# Measured on a real 2841-file Angular repo: 390/675 spec files (58%)
# classify into a layer (141 presentation, 118 application, 78
# infrastructure, 53 domain) — specs would be ~28% of all
# IN_LAYER-classified files. An ``application/*.use-case.spec.ts`` (rank
# 1) importing an infrastructure adapter (rank 2) to build a fake is
# exactly what those 118 application specs do — that trips
# ``loLayer.rank < hiLayer.rank`` and gets reported as a violation today.
# False positive by construction: a spec depending on ANYTHING to
# construct its test double is not a clean-architecture layering
# violation.
#
# Design decisions pinned by these tests (read-path only, NO re-ingest —
# a ``test: bool`` File prop is a deliberately separate future slice):
#
#   * Exclusion applies to BOTH endpoints (``lo`` AND ``hi``). A spec on
#     either side of the CALLS/REFERENCES/INJECTS edge removes the row.
#   * A PRODUCTION file depending on a SPEC file is ALSO excluded (not
#     just spec-depends-on-prod). Justification: a prod file importing a
#     test helper/fixture is a real code smell, but it is a DIFFERENT
#     problem (test code leaking into the production dependency graph)
#     than a clean-architecture layer-direction violation — conflating
#     the two under one report would mislabel the finding and there is
#     no separate detector for it today, so silently mis-reporting it as
#     a layer violation is worse than saying nothing.
#   * Matching is against the File node's ``key`` (already in the graph)
#     via ``code_memory.config.TEST_PATH_PATTERNS`` — see
#     test_architecture_config.py for the full pattern-set rationale
#     (case-sensitive substring containment; verified empirically against
#     this exact FalkorDB instance before being pinned).
#   * Row shape returned by ``layer_violations()`` is UNCHANGED — CLI
#     (``cli.py layer-violations``) and MCP
#     (``codememory_layer_violations``) both depend on the existing
#     ``{"file", "layer", "depends_on_file", "depends_on_layer",
#     "symbol"}`` shape.


def test_layer_violations_excludes_spec_file_depending_on_higher_layer(
    store: FalkorStore,
) -> None:
    """application/*.use-case.spec.ts (rank 1) importing an
    infrastructure adapter (rank 2) to build a test double — the exact
    118-application-specs scenario measured on the real repo. Must
    produce ZERO violations, not one."""
    store.upsert_nodes(
        [
            GraphNode(
                label="File",
                key="/repo/application/invoice.use-case.spec.ts",
                props={},
            ),
            GraphNode(
                label="File",
                key="/repo/infrastructure/invoice.adapter.ts",
                props={},
            ),
            GraphNode(
                label="Layer", key="l-app", props={"name": "application", "rank": 1}
            ),
            GraphNode(
                label="Layer",
                key="l-infra",
                props={"name": "infrastructure", "rank": 2},
            ),
            GraphNode(
                label="Symbol",
                key="/repo/infrastructure/invoice.adapter.ts::fetch#1",
                props={"name": "fetch"},
            ),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/application/invoice.use-case.spec.ts",
                dst_label="Layer",
                dst_key="l-app",
            ),
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/infrastructure/invoice.adapter.ts",
                dst_label="Layer",
                dst_key="l-infra",
            ),
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/repo/infrastructure/invoice.adapter.ts",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/invoice.adapter.ts::fetch#1",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/repo/application/invoice.use-case.spec.ts",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/invoice.adapter.ts::fetch#1",
            ),
        ]
    )

    assert store.layer_violations() == []


def test_layer_violations_still_flags_identical_dependency_between_two_production_files(
    store: FalkorStore,
) -> None:
    """Sanity/regression guard: the SAME dependency shape as the test
    above, but with the ``.spec.ts`` file renamed to a plain production
    module — proves the exclusion targets the spec MARKER, not the
    application/infrastructure layer pairing itself. Must still report
    exactly ONE violation."""
    store.upsert_nodes(
        [
            GraphNode(
                label="File",
                key="/repo/application/invoice.use-case.ts",
                props={},
            ),
            GraphNode(
                label="File",
                key="/repo/infrastructure/invoice.adapter.ts",
                props={},
            ),
            GraphNode(
                label="Layer", key="l-app", props={"name": "application", "rank": 1}
            ),
            GraphNode(
                label="Layer",
                key="l-infra",
                props={"name": "infrastructure", "rank": 2},
            ),
            GraphNode(
                label="Symbol",
                key="/repo/infrastructure/invoice.adapter.ts::fetch#1",
                props={"name": "fetch"},
            ),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/application/invoice.use-case.ts",
                dst_label="Layer",
                dst_key="l-app",
            ),
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/infrastructure/invoice.adapter.ts",
                dst_label="Layer",
                dst_key="l-infra",
            ),
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/repo/infrastructure/invoice.adapter.ts",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/invoice.adapter.ts::fetch#1",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/repo/application/invoice.use-case.ts",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/invoice.adapter.ts::fetch#1",
            ),
        ]
    )

    violations = store.layer_violations()

    assert len(violations) == 1
    v = violations[0]
    assert v["file"] == "/repo/application/invoice.use-case.ts"
    assert v["layer"] == "application"
    assert v["depends_on_file"] == "/repo/infrastructure/invoice.adapter.ts"
    assert v["depends_on_layer"] == "infrastructure"
    assert v["symbol"] == "fetch"


def test_layer_violations_excludes_when_spec_is_on_the_dependency_target_side_only(
    store: FalkorStore,
) -> None:
    """Endpoint symmetry: a PRODUCTION domain file depending on a SPEC
    file classified into infrastructure must also be excluded — pins the
    "prod depending on a spec is a different problem, not a layer
    violation" design decision from a genuine production file's
    perspective (distinct from the two tests above, which vary the
    DEPENDENT/``lo`` side; this one varies the TARGET/``hi`` side)."""
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/domain/invoice.ts", props={}),
            GraphNode(
                label="File",
                key="/repo/infrastructure/invoice.adapter.spec.ts",
                props={},
            ),
            GraphNode(label="Layer", key="l-domain", props={"name": "domain", "rank": 0}),
            GraphNode(
                label="Layer",
                key="l-infra",
                props={"name": "infrastructure", "rank": 2},
            ),
            GraphNode(
                label="Symbol",
                key="/repo/infrastructure/invoice.adapter.spec.ts::helper#1",
                props={"name": "helper"},
            ),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/domain/invoice.ts",
                dst_label="Layer",
                dst_key="l-domain",
            ),
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/infrastructure/invoice.adapter.spec.ts",
                dst_label="Layer",
                dst_key="l-infra",
            ),
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/repo/infrastructure/invoice.adapter.spec.ts",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/invoice.adapter.spec.ts::helper#1",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/repo/domain/invoice.ts",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/invoice.adapter.spec.ts::helper#1",
            ),
        ]
    )

    assert store.layer_violations() == []


def test_layer_violations_still_flags_production_file_named_like_over_matching_guards(
    store: FalkorStore,
) -> None:
    """Over-matching guard, exercised end-to-end through
    ``layer_violations()`` rather than just the pure-Python pattern test
    in test_architecture_config.py: production files legitimately named
    ``spectrum.service.ts`` and ``contest.ts`` must NOT be excluded — the
    dependency between them must still be flagged as a violation."""
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/domain/contest.ts", props={}),
            GraphNode(
                label="File", key="/repo/infrastructure/spectrum.service.ts", props={}
            ),
            GraphNode(label="Layer", key="l-domain", props={"name": "domain", "rank": 0}),
            GraphNode(
                label="Layer",
                key="l-infra",
                props={"name": "infrastructure", "rank": 2},
            ),
            GraphNode(
                label="Symbol",
                key="/repo/infrastructure/spectrum.service.ts::read#1",
                props={"name": "read"},
            ),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/domain/contest.ts",
                dst_label="Layer",
                dst_key="l-domain",
            ),
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/infrastructure/spectrum.service.ts",
                dst_label="Layer",
                dst_key="l-infra",
            ),
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/repo/infrastructure/spectrum.service.ts",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/spectrum.service.ts::read#1",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/repo/domain/contest.ts",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/spectrum.service.ts::read#1",
            ),
        ]
    )

    violations = store.layer_violations()

    assert len(violations) == 1
    assert violations[0]["file"] == "/repo/domain/contest.ts"
    assert violations[0]["depends_on_file"] == "/repo/infrastructure/spectrum.service.ts"


def test_layer_violations_excludes_dotnet_tests_project_dependency(
    store: FalkorStore,
) -> None:
    """.NET convention: a file inside a ``Billing.Tests`` project folder,
    named with the ``*Tests.cs`` suffix, depending on a higher-rank
    production layer must not be flagged."""
    store.upsert_nodes(
        [
            GraphNode(
                label="File", key="/repo/Billing.Tests/InvoiceTests.cs", props={}
            ),
            GraphNode(
                label="File", key="/repo/Billing.Infrastructure/InvoiceRepo.cs", props={}
            ),
            GraphNode(label="Layer", key="l-app", props={"name": "application", "rank": 1}),
            GraphNode(
                label="Layer",
                key="l-infra",
                props={"name": "infrastructure", "rank": 2},
            ),
            GraphNode(
                label="Symbol",
                key="/repo/Billing.Infrastructure/InvoiceRepo.cs::Save#1",
                props={"name": "Save"},
            ),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/Billing.Tests/InvoiceTests.cs",
                dst_label="Layer",
                dst_key="l-app",
            ),
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/Billing.Infrastructure/InvoiceRepo.cs",
                dst_label="Layer",
                dst_key="l-infra",
            ),
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/repo/Billing.Infrastructure/InvoiceRepo.cs",
                dst_label="Symbol",
                dst_key="/repo/Billing.Infrastructure/InvoiceRepo.cs::Save#1",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/repo/Billing.Tests/InvoiceTests.cs",
                dst_label="Symbol",
                dst_key="/repo/Billing.Infrastructure/InvoiceRepo.cs::Save#1",
            ),
        ]
    )

    assert store.layer_violations() == []


def test_layer_violations_excludes_python_test_prefixed_and_suffixed_files(
    store: FalkorStore,
) -> None:
    """Python conventions: both ``test_invoice.py`` (prefix) and
    ``invoice_test.py`` (suffix) depending on a higher-rank layer must be
    excluded — proven with two independent violation shapes in the same
    graph so both patterns are exercised end-to-end through
    ``layer_violations()`` in one query."""
    store.upsert_nodes(
        [
            GraphNode(label="File", key="/repo/application/test_invoice.py", props={}),
            GraphNode(label="File", key="/repo/application/invoice_test.py", props={}),
            GraphNode(
                label="File", key="/repo/infrastructure/invoice_repo.py", props={}
            ),
            GraphNode(label="Layer", key="l-app", props={"name": "application", "rank": 1}),
            GraphNode(
                label="Layer",
                key="l-infra",
                props={"name": "infrastructure", "rank": 2},
            ),
            GraphNode(
                label="Symbol",
                key="/repo/infrastructure/invoice_repo.py::save#1",
                props={"name": "save"},
            ),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/application/test_invoice.py",
                dst_label="Layer",
                dst_key="l-app",
            ),
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/application/invoice_test.py",
                dst_label="Layer",
                dst_key="l-app",
            ),
            GraphEdge(
                type="IN_LAYER",
                src_label="File",
                src_key="/repo/infrastructure/invoice_repo.py",
                dst_label="Layer",
                dst_key="l-infra",
            ),
            GraphEdge(
                type="DEFINES",
                src_label="File",
                src_key="/repo/infrastructure/invoice_repo.py",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/invoice_repo.py::save#1",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/repo/application/test_invoice.py",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/invoice_repo.py::save#1",
            ),
            GraphEdge(
                type="CALLS",
                src_label="File",
                src_key="/repo/application/invoice_test.py",
                dst_label="Symbol",
                dst_key="/repo/infrastructure/invoice_repo.py::save#1",
            ),
        ]
    )

    assert store.layer_violations() == []


def test_layer_violations_return_row_shape_is_unchanged_by_the_exclusion(
    store: FalkorStore,
) -> None:
    """The CLI (``cli.py layer-violations``) and the MCP tool
    (``codememory_layer_violations``) both depend on this exact row
    shape — the spec-exclusion fix must not add, rename, or drop keys."""
    _seed_layered_files(store)

    violations = store.layer_violations()

    assert len(violations) == 1
    assert set(violations[0].keys()) == {
        "file",
        "layer",
        "depends_on_file",
        "depends_on_layer",
        "symbol",
    }
