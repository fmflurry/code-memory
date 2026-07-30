"""Tests for ``Pipeline._index_architecture`` — the Layer-node projection
pass, modelled on ``_index_file_containment`` (pipeline.py:967-1036).

New symbol under test (does not exist yet — RED until implemented):

    Pipeline._index_architecture(
        self, root: Path, stats: IngestStats, *,
        head_sha: str | None = None, head_ord: int | None = None,
    ) -> None

Contract pinned down by these tests:

* Reads ``File`` nodes from ``getattr(self, "_active_graph", self.graph)``
  via a plain ``MATCH (f:File) RETURN f.key`` query (no ``f.lang`` filter —
  unlike ``_index_file_containment``, this pass applies to every
  language, not just .NET).
* For each file, classifies it via ``classify_file_layer`` (imported from
  ``code_memory.config``). Files with no match are skipped entirely.
* Writes one ``Layer`` node per distinct ``(context, name)`` pair, keyed
  ``f"{root}::layer::{context}::{name}"`` with props ``name`` and ``rank``.
  THE KEY MUST NOT START WITH ``name::`` — that prefix is the resolver's
  placeholder namespace (``resolver.py`` ``PLACEHOLDER_PREFIX``) and
  ``_cleanup_orphans`` (resolver.py:803) would eventually delete anything
  living there.
* Writes a ``File -[:IN_LAYER]-> Layer`` edge per classified file.
* Threads ``head_sha`` / ``head_ord`` into both ``upsert_nodes`` and
  ``upsert_edges`` calls, exactly like every other ``_index_*`` pass.
* No files at all (or none that classify) is a silent no-op — no upsert
  calls at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from code_memory.orchestrator import pipeline as pipeline_mod
from code_memory.orchestrator.pipeline import IngestStats, Pipeline


def _init_git_repo(root: Path) -> None:
    """Real git roundtrip with a fresh repo + one commit so
    ``_resolve_head`` (called internally by ``_ingest_full``) returns a
    non-``None`` head SHA. Same pattern as
    ``test_pipeline_temporal_wiring.py::test_resolve_head_returns_sha_and_ord_in_git_repo``
    — reused here rather than inventing a new fixture."""

    def run(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
        )

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "test")
    (root / "a.txt").write_text("hello")
    run("add", "a.txt")
    run("commit", "-qm", "init")


class _Result:
    def __init__(self, rows: list[list[Any]]) -> None:
        self.result_set = rows


class _FakeArchitectureGraph:
    """Stand-in for FalkorStore. ``.graph`` is a self-reference so
    ``active.graph.query(...)`` (the pattern _index_file_containment uses)
    resolves back onto this same object, mirroring the ``_RecordingGraph``
    trick in test_pipeline_temporal_wiring.py."""

    def __init__(self, file_keys: list[str]) -> None:
        self._file_keys = file_keys
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.upserted_nodes: list[Any] = []
        self.node_calls: list[dict[str, Any]] = []
        self.upserted_edges: list[Any] = []
        self.edge_calls: list[dict[str, Any]] = []
        self.sweep_calls: list[dict[str, Any]] = []

    @property
    def graph(self) -> "_FakeArchitectureGraph":
        return self

    def query(self, q: str, params: dict[str, Any] | None = None) -> _Result:
        self.queries.append((q, params or {}))
        return _Result([[k] for k in self._file_keys])

    def upsert_nodes(
        self, nodes: Any, *, head_sha: str | None = None, head_ord: int | None = None
    ) -> None:
        node_list = list(nodes)
        self.upserted_nodes.extend(node_list)
        self.node_calls.append({"head_sha": head_sha, "head_ord": head_ord, "n": len(node_list)})

    def upsert_edges(
        self, edges: Any, *, head_sha: str | None = None, head_ord: int | None = None
    ) -> None:
        edge_list = list(edges)
        self.upserted_edges.extend(edge_list)
        self.edge_calls.append({"head_sha": head_sha, "head_ord": head_ord, "n": len(edge_list)})

    def delete_file(
        self, path: str, *, head_sha: str | None = None, head_ord: int | None = None
    ) -> None:
        """Only exercised by the delta-ingest wiring tests below."""
        return None

    def sweep_stale(
        self, label: str, head_sha: str, head_ord: int | None = None
    ) -> int:
        """Defect 2 (HIGH) fix target: real FalkorStore.sweep_stale
        tombstones ``label`` nodes not reconfirmed at ``head_sha``. The
        fake only records the call so wiring tests can assert
        ``_index_architecture`` actually invokes it — the *effect*
        (invalid_sha actually getting set on an orphaned Layer node) is
        proven separately against real FalkorDB in
        test_falkor_layer_queries.py, which is the only place fake-graph
        recording can't substitute for real MERGE/SET semantics."""
        self.sweep_calls.append(
            {"label": label, "head_sha": head_sha, "head_ord": head_ord}
        )
        return 0


def _bare_pipeline() -> Pipeline:
    return Pipeline.__new__(Pipeline)


# ------------------------------------------------------------- no files


def test_index_architecture_is_noop_when_graph_has_no_files() -> None:
    pipe = _bare_pipeline()
    fake = _FakeArchitectureGraph([])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    assert fake.upserted_nodes == []
    assert fake.upserted_edges == []


def test_index_architecture_skips_files_with_no_layer_match() -> None:
    pipe = _bare_pipeline()
    fake = _FakeArchitectureGraph(["/repo/src/utils/helpers.ts"])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    assert fake.upserted_nodes == []
    assert fake.upserted_edges == []


# ---------------------------------------------------------- key + props


def test_index_architecture_layer_key_is_layer_namespaced_not_name_namespaced() -> None:
    """Regression guard: the resolver's placeholder namespace is
    ``name::``; a Layer node key starting with that prefix would be a
    silent target for _cleanup_orphans's STARTS WITH sweep.

    UPDATED for FIX 1: ``_index_architecture`` now also upserts a
    context-carrying ``File`` node alongside the ``Layer`` node (see the
    FIX 1 section below), so this filters to the Layer-labeled node
    specifically instead of assuming it's the only thing upserted — and
    the Layer's own props now include ``context``."""
    pipe = _bare_pipeline()
    fake = _FakeArchitectureGraph(
        ["/repo/src/app/features/invoicing/domain/invoice.ts"]
    )
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    layer_nodes = [n for n in fake.upserted_nodes if n.label == "Layer"]
    assert len(layer_nodes) == 1
    node = layer_nodes[0]
    assert node.key == "/repo::layer::invoicing::domain"
    assert not node.key.startswith("name::")
    assert node.key.split("::")[1] == "layer"
    assert node.props == {"name": "domain", "rank": 0, "context": "invoicing"}


def test_index_architecture_writes_in_layer_edge_from_file_to_layer() -> None:
    pipe = _bare_pipeline()
    file_key = "/repo/src/app/features/invoicing/domain/invoice.ts"
    fake = _FakeArchitectureGraph([file_key])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    assert len(fake.upserted_edges) == 1
    edge = fake.upserted_edges[0]
    assert edge.type == "IN_LAYER"
    assert edge.src_label == "File"
    assert edge.src_key == file_key
    assert edge.dst_label == "Layer"
    assert edge.dst_key == "/repo::layer::invoicing::domain"


# --------------------------------------------------------------- dedup


def test_index_architecture_dedupes_layer_node_across_multiple_files() -> None:
    """Two files that land in the same (context, layer) must share ONE
    Layer node but each get their own IN_LAYER edge.

    UPDATED for FIX 1: filters to Layer-labeled nodes only — with the
    context-carrying File-node upsert added, 2 classified files now
    produce 2 additional File-node upserts alongside the 1 shared Layer
    node, not just 1 node total."""
    pipe = _bare_pipeline()
    files = [
        "/repo/src/app/features/invoicing/domain/invoice.ts",
        "/repo/src/app/features/invoicing/domain/line-item.ts",
    ]
    fake = _FakeArchitectureGraph(files)
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    layer_nodes = [n for n in fake.upserted_nodes if n.label == "Layer"]
    assert len(layer_nodes) == 1
    assert len(fake.upserted_edges) == 2
    assert {e.src_key for e in fake.upserted_edges} == set(files)


# ------------------------------------------------- FIX 1: context stored
# as a node property
#
# ``_index_architecture`` computes ``context`` (pipeline.py:1110) and
# then throws it away — the Layer props dict was only
# ``{"name", "rank"}``. The only way to recover a bounded-context name
# was ``key.split("::")[2]`` string surgery. Fix: add ``context`` to the
# Layer props, AND stamp ``context`` onto the File node too (so "which
# files/contexts does this touch" doesn't require re-deriving it from
# the Layer key). No new store method is being specified here — these
# tests assert on the upserted node props only.


def test_index_architecture_layer_node_context_prop_matches_key_segment() -> None:
    pipe = _bare_pipeline()
    file_key = "/repo/src/app/features/invoicing/domain/invoice.ts"
    fake = _FakeArchitectureGraph([file_key])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    layer_nodes = [n for n in fake.upserted_nodes if n.label == "Layer"]
    assert len(layer_nodes) == 1
    assert layer_nodes[0].props["context"] == "invoicing"
    assert layer_nodes[0].key.split("::")[2] == layer_nodes[0].props["context"]


# ---------------------------------------- HIGH (round 3): File-node context
# write must NOT go through upsert_nodes / GraphNode
#
# ``_index_architecture`` re-derives classification for ALL live files
# EVERY run (not just ones actually touched this ingest). Routing the
# context write through ``GraphNode(label="File", ...)`` +
# ``upsert_nodes`` means every classified file's ``last_seen_sha`` /
# ``last_seen_ord`` gets bumped to the CURRENT run's head — even a file
# that was never reparsed this ingest — because ``upsert_nodes`` does
# ``ON MATCH SET n.last_seen_sha = $head, n.last_seen_ord = $ord``
# unconditionally (falkor_store.py:152-170). That breaks the "last_seen
# = last ingest that actually saw this node" invariant, and since
# ``"File"`` is sweepable, a future ``sweep_stale("File", …)`` or drift
# check would never fire for any classified file.
#
# Fix: use a plain ``SET f.context = $context`` query — mirroring the
# NULL-clear query ALREADY used for unclassified files
# (pipeline.py:1157-1160) — NOT ``GraphNode``/``upsert_nodes``. These
# tests replace the earlier (now-superseded) versions that asserted a
# File-labelled node WOULD appear in ``upserted_nodes`` — that was
# exactly the mechanism this fix removes. The real temporal-stamp
# preservation assertion (an untouched file's last_seen_sha/ord staying
# put across a later ingest) needs real MERGE/SET semantics and lives in
# test_falkor_layer_queries.py.


def test_index_architecture_file_context_write_does_not_use_upsert_nodes() -> None:
    """No File-labelled node may ever appear in ``upserted_nodes`` for
    context purposes — context must be written via a plain targeted
    query instead, which never touches ``last_seen_sha``/``last_seen_ord``."""
    pipe = _bare_pipeline()
    file_key = "/repo/src/app/features/invoicing/domain/invoice.ts"
    fake = _FakeArchitectureGraph([file_key])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    assert [n for n in fake.upserted_nodes if n.label == "File"] == [], (
        "context must not be written via GraphNode/upsert_nodes — that "
        "silently bumps last_seen_sha/last_seen_ord for every classified "
        "file on every run, even ones never reparsed this ingest"
    )


def test_index_architecture_writes_file_context_via_plain_set_query() -> None:
    pipe = _bare_pipeline()
    file_key = "/repo/src/app/features/invoicing/domain/invoice.ts"
    fake = _FakeArchitectureGraph([file_key])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    context_set_queries = [
        (q, params)
        for q, params in fake.queries
        if "SET f.context" in q or "SET f.context" in q.replace("\n", " ")
    ]
    # Exclude the NULL-clear query (unclassified files) — this file
    # classifies, so we want the write path, not the clear path.
    write_queries = [
        (q, params) for q, params in context_set_queries if "NULL" not in q
    ]
    assert write_queries, (
        f"expected a plain SET f.context write query; saw queries: "
        f"{[q for q, _ in fake.queries]}"
    )
    q, params = write_queries[-1]
    # Accept either a per-file or UNWIND-batched shape — just confirm
    # the file key and computed context ("invoicing") actually appear
    # in the query parameters somewhere.
    flattened = str(params)
    assert file_key in flattened
    assert "invoicing" in flattened


def test_index_architecture_unclassified_file_gets_no_context_write_at_all() -> None:
    """A file with no layer match must not get ANY context-related
    query at all (neither an upsert_nodes call nor a bare SET) —
    distinct from the NULL-clear path, which only fires for a file that
    USED to classify and no longer does (covered separately in
    test_falkor_layer_queries.py)."""
    pipe = _bare_pipeline()
    file_key = "/repo/src/utils/helpers.ts"
    fake = _FakeArchitectureGraph([file_key])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    assert [n for n in fake.upserted_nodes if n.label == "File"] == []
    context_write_queries = [
        (q, params)
        for q, params in fake.queries
        if "SET f.context" in q and "NULL" not in q
    ]
    assert context_write_queries == [], (
        "an unclassified file must get no context write query — it "
        "never classified, so there's nothing to clear or set"
    )


# --------------------------------------------------------- no lang filter


def test_index_architecture_query_does_not_filter_by_language() -> None:
    """Unlike _index_file_containment (.NET-only), this pass must apply
    to every language — no ``WHERE f.lang IN [...]`` restriction."""
    pipe = _bare_pipeline()
    fake = _FakeArchitectureGraph([])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    assert fake.queries, "expected _index_architecture to query File nodes"
    query_text = fake.queries[0][0]
    assert "MATCH (f:File)" in query_text
    assert "f.lang" not in query_text


# ------------------------------------------------- Defect 1 (HIGH): root-
# relative classification, not raw absolute file_key
#
# File node keys are ``str(p.resolve())`` — always absolute. Classifying
# on the FULL absolute key means any ancestor directory OUTSIDE the
# ingest root that happens to be literally spelled ``Api``/``Core``/
# ``Domain``/``Application``/``Infrastructure``/``Presentation`` (a
# checkout path like ``.../Workspace/Domain/myrepo/...``, or a company
# name that happens to contain one of those words as a whole path
# segment) leaks into the classification of files that have nothing to
# do with that layer, with a bogus ``context`` lifted from the unrelated
# ancestor. Fix: compute the file's path RELATIVE to ``root`` (already a
# parameter of ``_index_architecture``) and classify THAT, never the raw
# ``file_key``.


def test_index_architecture_classifies_relative_to_root_not_full_absolute_path() -> (
    None
):
    """The money test. ``root`` itself lives under an ancestor folder
    literally named ``Domain`` — a real-world checkout layout, not a
    contrived edge case. The file is an ordinary utils module with NO
    layer segment anywhere in its path relative to ``root``. Classifying
    on the raw absolute key would match the ancestor ``Domain`` segment
    and wrongly project a Layer node; classifying relative to ``root``
    must see only ``src/utils/foo.py`` and find no match."""
    pipe = _bare_pipeline()
    root = Path("/Users/alice/Workspace/Domain/myrepo")
    file_key = str(root / "src" / "utils" / "foo.py")
    fake = _FakeArchitectureGraph([file_key])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(root, stats, head_sha="sha1", head_ord=1)

    assert fake.upserted_nodes == []
    assert fake.upserted_edges == []


def test_index_architecture_skips_file_key_not_under_root_without_crashing() -> None:
    """A File key that isn't under ``root`` at all (e.g. stale rows from
    a prior ingest of a different checkout path, or a monorepo scan that
    picked up a sibling project) must be skipped — no Layer/edge, and no
    exception from a failed relative-path computation."""
    pipe = _bare_pipeline()
    root = Path("/repo")
    file_key = "/var/other-project/src/domain/thing.py"
    fake = _FakeArchitectureGraph([file_key])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(root, stats, head_sha="sha1", head_ord=1)

    assert fake.upserted_nodes == []
    assert fake.upserted_edges == []


def test_index_architecture_still_classifies_vendored_node_modules_relative_path() -> (
    None
):
    """Decision for the vendored-path question raised alongside Defect 1:
    once a File node exists under ``root``, ``_index_architecture``
    classifies it purely by its path RELATIVE to root — it does not
    special-case ``node_modules`` (or any other vendor directory). Env
    excluding vendored directories is the ingest WALKER's job
    (``Extractor.DEFAULT_IGNORE_DIRS`` in treesitter.py already lists
    ``node_modules``, so in normal operation such a File node is never
    even created), not the classifier's. This test pins the classifier's
    own contract independent of that upstream filtering — it must not
    grow special-case vendor-dir exclusion logic of its own.

    UPDATED for FIX 1: filters to Layer-labeled nodes only and expects
    ``context`` in props — ``_index_architecture`` now also upserts a
    context-carrying File node per classified file, same modernisation
    already applied to this file's other node-shape assertions."""
    pipe = _bare_pipeline()
    root = Path("/repo")
    file_key = str(root / "node_modules" / "pkg" / "domain" / "x.js")
    fake = _FakeArchitectureGraph([file_key])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(root, stats, head_sha="sha1", head_ord=1)

    layer_nodes = [n for n in fake.upserted_nodes if n.label == "Layer"]
    assert len(layer_nodes) == 1
    assert layer_nodes[0].props == {"name": "domain", "rank": 0, "context": "pkg"}
    assert len(fake.upserted_edges) == 1
    assert fake.upserted_edges[0].src_key == file_key
    assert fake.upserted_edges[0].dst_key == "/repo::layer::pkg::domain"


# ------------------------------------------------------- temporal stamping


def test_index_architecture_threads_head_sha_and_head_ord() -> None:
    pipe = _bare_pipeline()
    fake = _FakeArchitectureGraph(
        ["/repo/src/app/features/invoicing/domain/invoice.ts"]
    )
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="deadbeef", head_ord=42)

    assert fake.node_calls
    assert fake.edge_calls
    for call in fake.node_calls + fake.edge_calls:
        assert call["head_sha"] == "deadbeef"
        assert call["head_ord"] == 42


# -------------------------------------------------------- shadow routing


def test_index_architecture_writes_to_active_graph_during_shadow_ingest() -> None:
    """During a full rebuild, Pipeline._active_graph points at the shadow
    FalkorStore; _index_architecture must write there, never to the live
    ``self.graph`` — same contract as every other _index_* pass."""
    pipe = _bare_pipeline()
    live = _FakeArchitectureGraph(["/repo/src/Domain/Foo.cs"])
    shadow = _FakeArchitectureGraph(["/repo/src/Domain/Foo.cs"])
    pipe.graph = live  # type: ignore[assignment]
    pipe._active_graph = shadow  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    assert shadow.upserted_nodes, "expected the shadow graph to receive the Layer node"
    assert shadow.upserted_edges, "expected the shadow graph to receive the IN_LAYER edge"
    assert live.upserted_nodes == []
    assert live.upserted_edges == []
    assert live.queries == [], "the live graph must never be queried during shadow ingest"


# --------------------------------------------- Defect 2 (HIGH): sweep_stale
# never called for the "Layer" label
#
# ``delete_file`` tombstones a deleted File, its DEFINES-linked Symbols,
# and every edge touching it (including its IN_LAYER edge) — but never
# the ``Layer`` node itself. ``vacuum``'s eligibility predicate requires
# ``invalid_sha IS NOT NULL``, and nothing else ever sets that for a
# Layer node, so an orphaned Layer is permanently un-collectible. Fix:
# ``_index_architecture`` must call
# ``active.sweep_stale("Layer", head_sha, head_ord)`` after its upserts,
# on every call — including when this run finds zero classifiable files
# (a delete-only delta) and even when the wiring for a legacy non-git
# ingest passes ``head_sha=None`` (in which case sweep_stale, which
# requires a non-optional ``head_sha``, must NOT be invoked at all).
#
# These tests only prove the WIRING (sweep_stale gets called with the
# right args) against the fake graph — the actual tombstoning *effect*
# needs real MERGE/SET semantics and lives in
# test_falkor_layer_queries.py.


def test_index_architecture_calls_sweep_stale_on_layer_label() -> None:
    pipe = _bare_pipeline()
    file_key = "/repo/src/app/features/invoicing/domain/invoice.ts"
    fake = _FakeArchitectureGraph([file_key])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    assert fake.sweep_calls == [{"label": "Layer", "head_sha": "sha1", "head_ord": 1}]


def test_index_architecture_sweeps_layer_even_when_no_files_classify() -> None:
    """Delete-only delta: no File classifies this run (or none exist),
    but Layer nodes orphaned by a PRIOR run must still get swept — the
    sweep must not be gated behind the files/edges early-return."""
    pipe = _bare_pipeline()
    fake = _FakeArchitectureGraph([])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    assert fake.sweep_calls == [{"label": "Layer", "head_sha": "sha1", "head_ord": 1}]


def test_index_architecture_does_not_sweep_when_head_sha_is_none() -> None:
    """``sweep_stale`` requires a non-optional ``head_sha`` str. A legacy
    non-git ingest (``head_sha=None``) must not call it at all."""
    pipe = _bare_pipeline()
    fake = _FakeArchitectureGraph(["/repo/src/domain/x.ts"])
    pipe.graph = fake  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha=None, head_ord=None)

    assert fake.sweep_calls == []


def test_index_architecture_sweeps_shadow_graph_not_live_during_full_rebuild() -> None:
    """Same shadow-routing contract as node/edge upserts: the sweep must
    run against ``_active_graph`` (the shadow), never the live graph."""
    pipe = _bare_pipeline()
    live = _FakeArchitectureGraph(["/repo/src/Domain/Foo.cs"])
    shadow = _FakeArchitectureGraph(["/repo/src/Domain/Foo.cs"])
    pipe.graph = live  # type: ignore[assignment]
    pipe._active_graph = shadow  # type: ignore[assignment]
    stats = IngestStats()

    pipe._index_architecture(Path("/repo"), stats, head_sha="sha1", head_ord=1)

    assert shadow.sweep_calls == [{"label": "Layer", "head_sha": "sha1", "head_ord": 1}]
    assert live.sweep_calls == []


# ----------------------------------------------- wiring into full ingest


def _stub_pipeline_for_ingest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Pipeline:
    monkeypatch.setattr(Pipeline, "__init__", lambda self, **kw: None)
    pipe = Pipeline()
    pipe.graph = _FakeArchitectureGraph([])  # type: ignore[assignment]
    pipe.vector = type(
        "V",
        (),
        {
            "upsert": lambda self, *a, **kw: None,
            "delete_by_path": lambda self, *a, **kw: None,
            "recreate_collection": lambda self, *a, **kw: None,
        },
    )()  # type: ignore[assignment]
    pipe.cfg = type(
        "C",
        (),
        {"qdrant_code": "code", "qdrant_episodes": "eps", "falkor_graph": "g"},
    )()  # type: ignore[assignment]
    pipe.skip_vectors = True
    pipe.state = type("S", (), {"clear": lambda self, *a, **kw: None})()  # type: ignore[assignment]

    from code_memory.embed import HybridVec, SparseVec

    def _embed(self: object, texts: list[str]) -> list[Any]:
        return [HybridVec(dense=[0.0] * 8, sparse=SparseVec(indices=[], values=[])) for _ in texts]

    pipe.embedder = type("E", (), {"embed": _embed, "embed_one": lambda self, t: None})()  # type: ignore[assignment]
    return pipe


def test_ingest_full_calls_index_architecture_when_not_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipe = _stub_pipeline_for_ingest(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []

    def _fake_index_architecture(self: Pipeline, root: Path, stats: Any, **kw: Any) -> None:
        calls.append({"root": root, **kw})

    monkeypatch.setattr(Pipeline, "_index_architecture", _fake_index_architecture)
    monkeypatch.setattr(Pipeline, "_ingest_dotnet_projects", lambda self, *a, **kw: None)
    monkeypatch.setattr(pipeline_mod.Extractor, "walk", lambda self, root: iter([]))
    monkeypatch.setattr(Pipeline, "_commit_shadow_collection", lambda self: None)
    monkeypatch.setattr(Pipeline, "_run_resolver_on", lambda self, graph, stats: None)
    monkeypatch.setattr(Pipeline, "_drop_collection_if_exists", lambda self, name: None)

    pipe._ingest_full(tmp_path, dry_run=False)

    assert len(calls) == 1
    assert calls[0]["root"] == tmp_path


def test_ingest_full_dry_run_skips_index_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipe = _stub_pipeline_for_ingest(tmp_path, monkeypatch)
    calls: list[Any] = []

    monkeypatch.setattr(
        Pipeline, "_index_architecture", lambda self, root, stats, **kw: calls.append(1)
    )
    monkeypatch.setattr(Pipeline, "_ingest_dotnet_projects", lambda self, *a, **kw: None)
    monkeypatch.setattr(pipeline_mod.Extractor, "walk", lambda self, root: iter([]))

    pipe._ingest_full(tmp_path, dry_run=True)

    assert calls == []


def test_ingest_delta_calls_index_architecture_even_for_delete_only_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete-only delta: stats.files stays 0, but the architecture pass
    must still run — a deleted file can change which Layer nodes are
    still referenced."""
    from code_memory.orchestrator import git_delta

    pipe = _stub_pipeline_for_ingest(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []

    def _fake_index_architecture(self: Pipeline, root: Path, stats: Any, **kw: Any) -> None:
        calls.append({"root": root, "files_at_call_time": stats.files, **kw})

    monkeypatch.setattr(Pipeline, "_index_architecture", _fake_index_architecture)
    monkeypatch.setattr(Pipeline, "_ingest_dotnet_projects", lambda self, *a, **kw: None)

    delta = git_delta.Delta(changed=[], deleted=[tmp_path / "old.ts"], dirty=[])
    stats = pipe._ingest_delta(
        tmp_path, delta, base_sha="base", head_sha="head", dry_run=False
    )

    assert stats.files == 0
    assert len(calls) == 1
    assert calls[0]["files_at_call_time"] == 0
    assert calls[0]["head_sha"] == "head"


def test_ingest_delta_dry_run_skips_index_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_memory.orchestrator import git_delta

    pipe = _stub_pipeline_for_ingest(tmp_path, monkeypatch)
    calls: list[Any] = []

    monkeypatch.setattr(
        Pipeline, "_index_architecture", lambda self, root, stats, **kw: calls.append(1)
    )
    monkeypatch.setattr(Pipeline, "_ingest_dotnet_projects", lambda self, *a, **kw: None)

    delta = git_delta.Delta(changed=[], deleted=[tmp_path / "old.ts"], dirty=[])
    pipe._ingest_delta(tmp_path, delta, base_sha="base", head_sha="head", dry_run=True)

    assert calls == []


# --------------------------------- Defect 2 (HIGH): real (unmocked) sweep
#
# The wiring tests above monkeypatch ``_index_architecture`` itself, so
# they only prove "the ingest path calls this method" — they say nothing
# about what the REAL method does once invoked. These two run the REAL
# ``_index_architecture`` through the real ``_ingest_full`` /
# ``_ingest_delta`` paths and assert the observable effect: sweep_stale
# actually fires with the right label/head_sha, not merely that a mock
# was invoked.


def test_ingest_full_real_index_architecture_sweeps_stale_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_ingest_full`` resolves ``head_sha`` itself via
    ``_resolve_head(root)`` (pipeline.py:551), which returns ``(None,
    None)`` for a non-git directory. A bare ``tmp_path`` is NOT a git
    repo, so without a real commit here this test would call
    ``_index_architecture`` with ``head_sha=None`` — the exact input
    ``test_index_architecture_does_not_sweep_when_head_sha_is_none``
    (above) asserts must NOT sweep. Those two tests would otherwise be
    unsatisfiable on identical input, so this one must give
    ``_resolve_head`` a real git repo + commit to work with."""
    _init_git_repo(tmp_path)
    pipe = _stub_pipeline_for_ingest(tmp_path, monkeypatch)
    file_key = str(tmp_path / "src" / "domain" / "x.ts")
    fake = _FakeArchitectureGraph([file_key])
    pipe.graph = fake  # type: ignore[assignment]
    monkeypatch.setattr(Pipeline, "_ingest_dotnet_projects", lambda self, *a, **kw: None)
    monkeypatch.setattr(pipeline_mod.Extractor, "walk", lambda self, root: iter([]))
    monkeypatch.setattr(Pipeline, "_commit_shadow_collection", lambda self: None)
    monkeypatch.setattr(Pipeline, "_run_resolver_on", lambda self, graph, stats: None)
    monkeypatch.setattr(Pipeline, "_drop_collection_if_exists", lambda self, name: None)

    pipe._ingest_full(tmp_path, dry_run=False)

    assert fake.sweep_calls, "expected the REAL _index_architecture to sweep stale Layer nodes"
    assert fake.sweep_calls[0]["label"] == "Layer"
    assert fake.sweep_calls[0]["head_sha"] is not None


def test_ingest_delta_real_index_architecture_sweeps_stale_layer_on_delete_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete-only delta (``stats.files`` stays 0) still runs the sweep —
    a deleted file can be the last file backing a Layer node.

    Verified this does NOT share the sibling ``_ingest_full`` test's
    non-git-tmp_path bug: unlike ``_ingest_full``, which calls
    ``_resolve_head(root)`` internally (pipeline.py:551) and would get
    ``(None, None)`` back from a bare ``tmp_path``, ``_ingest_delta``
    takes ``head_sha`` as a required, caller-supplied string parameter
    (pipeline.py:1264) and never calls ``is_git_repo``/``_resolve_head``
    on it. ``head_sha="head"`` below is not resolved against real git
    state at all — the only git call in this path is
    ``git_delta.commit_ordinal(root, head_sha)`` for ``head_ord``, which
    catches ``GitError`` and returns ``None`` (git_delta.py:90-91) rather
    than raising when ``root`` isn't a real repo. So this test's
    ``tmp_path`` not being a git repo is irrelevant to it — it already
    exercises the real ``_ingest_delta`` -> real ``_index_architecture``
    -> ``sweep_stale`` wiring with a genuine non-``None`` head_sha, on
    purpose, without needing ``_init_git_repo``. No production code
    assumption is being sidestepped here."""
    from code_memory.orchestrator import git_delta

    pipe = _stub_pipeline_for_ingest(tmp_path, monkeypatch)
    fake = _FakeArchitectureGraph([])  # no files remain, only stale Layer nodes server-side
    pipe.graph = fake  # type: ignore[assignment]
    monkeypatch.setattr(Pipeline, "_ingest_dotnet_projects", lambda self, *a, **kw: None)

    delta = git_delta.Delta(changed=[], deleted=[tmp_path / "old.ts"], dirty=[])
    stats = pipe._ingest_delta(
        tmp_path, delta, base_sha="base", head_sha="head", dry_run=False
    )

    assert stats.files == 0
    assert fake.sweep_calls, "expected sweep_stale to run even for a delete-only delta"
    assert fake.sweep_calls[0] == {"label": "Layer", "head_sha": "head", "head_ord": None}


# ------------------------------------------- Defect 5 (HIGH): dead IMPORTS
# arm of FalkorStore.layer_violations
#
# ``IMPORTS`` edges target ``:Module`` nodes (pipeline.py ``_upsert_graph``
# — every ``ex.imports`` entry becomes a bare ``GraphNode(label="Module",
# key=mod)`` with no props). Nothing anywhere in the ingest pipeline ever
# emits a ``DEFINES`` edge INTO a ``:Module`` node — ``DEFINES`` is only
# ever ``File -> Symbol`` (see the single ``type="DEFINES"`` emission
# site in ``_upsert_graph``). ``layer_violations()``'s join pattern is
# ``(lo)-[:IMPORTS|CALLS|REFERENCES|INJECTS]->(sym)<-[:DEFINES]-(hi)`` —
# for the IMPORTS arm to ever match, some file would need to ``DEFINES``
# a ``:Module`` node, which structurally never happens. So IMPORTS is
# dead code in that union: it can never contribute a single matched row,
# contradicting the docstring's claim that bare ``import``s are flagged.
#
# Investigated and confirmed via ``codememory`` graph search + direct
# read of ``pipeline.py`` (``_upsert_graph``'s Module/IMPORTS block) that
# no Module->File / File-DEFINES->Module linkage exists anywhere in the
# ingest pipeline today. Recommendation: option (b) — DROP ``IMPORTS``
# from the relationship union (and correct the docstring) rather than
# inventing a new Module->File linkage nothing else in the schema
# supports. This is a pure static/introspection assertion (no FalkorDB
# needed) so it lives here rather than in the live-Falkor test module.


def test_layer_violations_query_does_not_traverse_imports_edge_type() -> None:
    import inspect

    from code_memory.graph.falkor_store import FalkorStore

    source = inspect.getsource(FalkorStore.layer_violations)
    assert "IMPORTS" not in source, (
        "layer_violations must not traverse IMPORTS — it targets :Module "
        "nodes, and nothing in the ingest pipeline ever emits a DEFINES "
        "edge into a Module, so the arm can never match a row; keeping "
        "it in the relationship union is dead code that contradicts the "
        "tool's documented behaviour (option (b) from the Defect-5 "
        "review: drop IMPORTS from the union, correct the docstring)"
    )
