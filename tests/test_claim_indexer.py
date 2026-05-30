"""Tests for the Qdrant-backed claim indexer.

The indexer layers a semantic vector index on top of the SQLite claim
store. We exercise it with object-level fakes for both Qdrant and the
embedder so the suite stays fast and offline. The contract under test:

* ``upsert`` writes through to SQLite first, then embeds and pushes a
  Qdrant point with ``open=True``.
* When a single-valued predicate supersedes a prior claim, the prior
  point's ``open`` payload flips to ``False`` without re-embedding.
* Refreshing an existing open duplicate updates payload (e.g. raised
  ``confidence``) without paying for a new embed call.
* ``ensure_backfilled`` embeds pre-existing SQLite rows when Qdrant is
  empty — the lazy backfill path. Idempotent on a second call.
* ``search`` filters out closed (``open=False``) claims by default.
* When the vector backend raises on search, the indexer returns an
  empty list (the orchestrator then falls back to token overlap).
"""

from __future__ import annotations

from typing import Any

import pytest

from code_memory.claims import ClaimRecord
from code_memory.claims.indexer import ClaimsIndexer, _text_for
from code_memory.claims.store import ClaimsStore
from code_memory.embed.m3 import HybridVec, SparseVec
from code_memory.vector import VectorHit, VectorRecord


# ---------------------------------------------------------------- doubles


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_one(self, text: str) -> HybridVec:
        self.calls.append(text)
        base = [(ord(c) % 13) / 13.0 for c in text[:8]]
        while len(base) < 8:
            base.append(0.0)
        return HybridVec(dense=base, sparse=SparseVec(indices=[], values=[]))


class _FakeVector:
    """In-memory stand-in for QdrantStore.

    Tracks every interaction so tests can assert on side effects
    (collection creation, payload flips, embed counts). Mimics just
    enough of QdrantStore's surface for the indexer's needs.
    """

    def __init__(
        self,
        *,
        starts_missing: bool = True,
        search_raises: bool = False,
    ) -> None:
        self._exists = not starts_missing
        self._points: dict[str, VectorRecord] = {}
        self._search_raises = search_raises
        self.ensured: list[str] = []
        self.upserts: list[tuple[str, list[VectorRecord]]] = []
        self.set_payload_calls: list[tuple[str, list[str], dict[str, Any]]] = []
        # Filter passed to last ``search`` so we can assert default
        # filtering hides closed claims.
        self.last_search_filter: dict[str, Any] | None = None

    def _inspect_collection(self, name: str) -> str:
        return "hybrid" if self._exists else "missing"

    def ensure_collection(self, name: str) -> None:
        self.ensured.append(name)
        self._exists = True

    def count(self, name: str) -> int:
        return len(self._points) if self._exists else 0

    def upsert(self, collection: str, records: Any) -> None:
        records = list(records)
        self.upserts.append((collection, records))
        for rec in records:
            self._points[rec.id] = rec

    def set_payload(
        self, collection: str, ids: list[str], payload: dict[str, Any]
    ) -> None:
        self.set_payload_calls.append((collection, list(ids), dict(payload)))
        for pid in ids:
            rec = self._points.get(pid)
            if rec is not None:
                # Merge into existing payload (Qdrant's contract).
                merged = dict(rec.payload)
                merged.update(payload)
                self._points[pid] = VectorRecord(
                    id=rec.id, vector=rec.vector, payload=merged
                )

    def search(
        self,
        collection: str,
        vector: Any,
        top_k: int = 10,
        filt: dict[str, Any] | None = None,
        mode: str = "hybrid",
    ) -> list[VectorHit]:
        if self._search_raises:
            raise RuntimeError("simulated qdrant outage")
        self.last_search_filter = filt
        # Return all matching points; tests don't need real cosine.
        hits: list[VectorHit] = []
        for rec in self._points.values():
            if filt is not None:
                match = all(rec.payload.get(k) == v for k, v in filt.items())
                if not match:
                    continue
            hits.append(VectorHit(id=rec.id, score=0.9, payload=rec.payload))
        return hits[:top_k]


# ------------------------------------------------------------ helpers


def _claim(
    subject: str,
    predicate: str,
    obj: str,
    *,
    confidence: float = 0.9,
    valid_at: float = 1_700_000_000.0,
    evidence: str = "",
    polarity: bool = True,
) -> ClaimRecord:
    return ClaimRecord(
        subject=subject,
        predicate=predicate,
        object=obj,
        polarity=polarity,
        confidence=confidence,
        evidence_span=evidence or f"{subject} {predicate} {obj}",
        valid_at=valid_at,
    )


def _make_indexer(
    tmp_path,
    *,
    semantic_dedup_threshold: float = 1.01,
    **vector_kwargs,
) -> tuple[ClaimsIndexer, _FakeVector, _FakeEmbedder]:
    """Build an indexer wired to in-memory fakes.

    ``semantic_dedup_threshold`` defaults to ``1.01`` so the semantic
    near-duplicate path is disabled by default — the legacy tests below
    were written against the exact-triple SQL dedupe and would observe
    spurious collapses if the fake embedder's uniform 0.9 score tripped
    the threshold. Tests that exercise semantic dedup pass an explicit
    lower threshold.
    """
    store = ClaimsStore(path=tmp_path / "claims.db")
    vec = _FakeVector(**vector_kwargs)
    emb = _FakeEmbedder()
    indexer = ClaimsIndexer(
        store=store,
        vector=vec,
        embedder=emb,
        collection="claims__test",
        semantic_dedup_threshold=semantic_dedup_threshold,
    )
    return indexer, vec, emb


# ---------------------------------------------------------------- tests


def test_upsert_writes_sqlite_and_qdrant(tmp_path) -> None:
    """A fresh upsert lands one row in SQLite and one open point in Qdrant."""
    # Arrange
    indexer, vec, emb = _make_indexer(tmp_path)
    claim = _claim("project", "uses", "Postgres", evidence="we use Postgres in prod")

    # Act
    result = indexer.upsert(claim)

    # Assert
    assert result.was_new is True
    assert indexer.store.count() == 1
    # First upsert call is the lazy backfill (no rows yet); second is
    # the claim itself.
    upserted_ids = [r.id for _, recs in vec.upserts for r in recs]
    assert claim.id in upserted_ids
    point = vec._points[claim.id]
    assert point.payload["open"] is True
    assert point.payload["subject"] == "project"
    assert point.payload["object"] == "Postgres"
    # Evidence span is part of the embed text → embedder sees user
    # phrasing, not just the bare triple.
    assert any("we use Postgres in prod" in t for t in emb.calls)


def test_single_valued_predicate_flips_prior_open_false(tmp_path) -> None:
    """Switching tech stack closes the prior point via set_payload."""
    # Arrange
    indexer, vec, emb = _make_indexer(tmp_path)
    first = _claim("project", "uses", "MySQL", valid_at=10.0)
    second = _claim("project", "uses", "Postgres", valid_at=20.0)
    indexer.upsert(first)
    embed_calls_before = len(emb.calls)

    # Act — second claim contradicts the first on a single-valued predicate.
    indexer.upsert(second)

    # Assert — the prior point's open flag flipped to False without a re-embed.
    flips = [c for c in vec.set_payload_calls if c[2].get("open") is False]
    assert flips, "expected an open=False payload flip on supersession"
    closed_collection, closed_ids, _ = flips[-1]
    assert first.id in closed_ids
    assert vec._points[first.id].payload["open"] is False
    assert vec._points[second.id].payload["open"] is True
    # One additional embed call for the new claim, none for the close.
    assert len(emb.calls) == embed_calls_before + 1


def test_refresh_existing_skips_reembed(tmp_path) -> None:
    """Re-asserting the same triple bumps payload but doesn't re-embed."""
    # Arrange
    indexer, vec, emb = _make_indexer(tmp_path)
    weak = _claim("project", "uses", "Qdrant", confidence=0.7)
    indexer.upsert(weak)
    embed_calls_before = len(emb.calls)
    strong = _claim("project", "uses", "Qdrant", confidence=0.95)

    # Act
    result = indexer.upsert(strong)

    # Assert
    assert result.was_new is False
    assert len(emb.calls) == embed_calls_before  # no extra embed
    refresh_payload = vec.set_payload_calls[-1][2]
    assert refresh_payload["confidence"] == pytest.approx(0.95)
    assert refresh_payload["open"] is True


def test_ensure_backfilled_embeds_existing_rows(tmp_path) -> None:
    """SQLite has rows, Qdrant is empty → backfill embeds every row once."""
    # Arrange — write to the store directly, bypassing the indexer, so
    # the embedder never saw these claims.
    indexer, vec, emb = _make_indexer(tmp_path)
    raw_store = indexer.store
    raw_store.upsert(_claim("project", "uses", "Redis", valid_at=10.0))
    raw_store.upsert(_claim("user", "prefers", "dark mode", valid_at=20.0))
    # Sanity: indexer hasn't backfilled yet (no upsert via indexer.upsert).
    assert vec.upserts == []

    # Act
    embedded = indexer.ensure_backfilled()

    # Assert
    assert embedded == 2
    assert len(vec._points) == 2
    # Second call is idempotent: already-backfilled flag short-circuits.
    assert indexer.ensure_backfilled() == 0


def test_search_filters_closed_by_default(tmp_path) -> None:
    """search() must hide closed claims unless include_closed=True."""
    # Arrange
    indexer, vec, emb = _make_indexer(tmp_path)
    indexer.upsert(_claim("project", "uses", "MySQL", valid_at=10.0))
    indexer.upsert(_claim("project", "uses", "Postgres", valid_at=20.0))
    qvec = emb.embed_one("what db are we on")

    # Act
    hits = indexer.search(qvec, top_k=10)

    # Assert
    assert vec.last_search_filter == {"open": True}
    open_objects = {h.payload["object"] for h in hits}
    assert open_objects == {"Postgres"}

    # And the closed-included path returns both.
    hits_all = indexer.search(qvec, top_k=10, include_closed=True)
    assert {h.payload["object"] for h in hits_all} == {"MySQL", "Postgres"}


def test_search_returns_empty_when_collection_missing(tmp_path) -> None:
    """Missing collection → empty result (orchestrator falls back to tokens)."""
    # Arrange — vector starts missing AND we never call upsert/backfill.
    indexer, vec, emb = _make_indexer(tmp_path, starts_missing=True)
    qvec = emb.embed_one("anything")

    # Act
    hits = indexer.search(qvec)

    # Assert
    assert hits == []
    # No collection created — search shouldn't have side effects.
    assert vec.ensured == []


def test_search_swallows_backend_errors(tmp_path) -> None:
    """Qdrant outage → empty list, never propagated to the orchestrator."""
    # Arrange — collection exists so the missing-check passes, but
    # ``search`` raises to simulate a transient Qdrant failure.
    indexer, vec, emb = _make_indexer(
        tmp_path, starts_missing=False, search_raises=True
    )
    indexer.upsert(_claim("project", "uses", "Qdrant"))
    qvec = emb.embed_one("q")

    # Act
    hits = indexer.search(qvec)

    # Assert
    assert hits == []


def test_semantic_dedup_collapses_paraphrastic_claim(tmp_path) -> None:
    """A near-duplicate paraphrase refreshes the existing row, no new insert.

    Models the user-reported failure: a single utterance produced
    ``(project, uses, flurryx)`` AND ``(project, depends-on, flurryx)``
    as separate rows. With semantic dedup enabled, the second triple's
    embedding sits within threshold of the first and merges in place.
    """
    # Arrange — threshold below fake's uniform 0.9 score so any hit
    # within the polarity-filtered set counts as a near-duplicate.
    indexer, vec, emb = _make_indexer(tmp_path, semantic_dedup_threshold=0.85)
    first = _claim("project", "uses", "flurryx", confidence=0.85)
    indexer.upsert(first)
    embed_calls_after_first = len(emb.calls)
    points_after_first = set(vec._points.keys())

    # Act — paraphrastic restatement with a different predicate.
    second = _claim("project", "depends-on", "flurryx", confidence=0.85)
    result = indexer.upsert(second)

    # Assert — second claim merged into the first's row, no new row.
    assert result.was_new is False
    assert result.claim_id == first.id
    assert indexer.store.count() == 1
    # No new Qdrant point was created; only payload was refreshed.
    assert set(vec._points.keys()) == points_after_first
    # One embed call for the second claim's similarity probe; no second
    # embed for an "insert" path that never ran.
    assert len(emb.calls) == embed_calls_after_first + 1


def test_semantic_dedup_skipped_when_exact_triple_matches(tmp_path) -> None:
    """Exact (s,p,o) match falls through to SQL dedupe, preserving its semantics."""
    # Arrange
    indexer, vec, emb = _make_indexer(tmp_path, semantic_dedup_threshold=0.85)
    first = _claim("project", "uses", "flurryx", confidence=0.7)
    indexer.upsert(first)

    # Act — identical triple, stronger confidence.
    second = _claim("project", "uses", "flurryx", confidence=0.95)
    result = indexer.upsert(second)

    # Assert — same row id, single SQLite row, confidence raised via the
    # SQL refresh path (not the semantic one). The refresh payload
    # update is the canonical signal.
    assert result.claim_id == first.id
    assert indexer.store.count() == 1
    refreshed = indexer.store.by_id(first.id)
    assert refreshed is not None
    assert refreshed.confidence == pytest.approx(0.95)


def test_semantic_dedup_disabled_when_threshold_at_one(tmp_path) -> None:
    """Threshold ``>= 1.0`` disables the feature without touching the embedder."""
    # Arrange
    indexer, vec, emb = _make_indexer(tmp_path, semantic_dedup_threshold=1.0)
    first = _claim("project", "uses", "flurryx")
    indexer.upsert(first)
    second = _claim("project", "depends-on", "flurryx")

    # Act
    result = indexer.upsert(second)

    # Assert — second claim is a brand-new row, not a merge.
    assert result.was_new is True
    assert indexer.store.count() == 2


def test_semantic_dedup_swallows_search_backend_failure(tmp_path) -> None:
    """A search-time backend hiccup must not block the SQLite write.

    The semantic-dedup probe uses ``vector.search``. If Qdrant is
    transiently unavailable, we fall back to the insert path instead of
    raising — the SQLite row is still the authoritative truth and the
    indexer's normal "Qdrant temporarily behind" recovery applies.
    """
    # Arrange — first claim lands normally; second claim's dedup probe
    # then hits a vector backend that has started raising on search.
    indexer, vec, emb = _make_indexer(tmp_path, semantic_dedup_threshold=0.85)
    indexer.upsert(_claim("project", "uses", "flurryx"))
    vec._search_raises = True  # flip the fake into "outage" mode

    # Act
    second = _claim("project", "depends-on", "flurryx")
    result = indexer.upsert(second)

    # Assert — dedup short-circuits to None, normal insert path runs.
    assert result.was_new is True
    assert indexer.store.count() == 2


def test_text_for_includes_polarity_and_evidence() -> None:
    """The embed-text helper carries negation + raw user phrasing."""
    # Arrange
    affirm = _claim(
        "project", "uses", "Postgres", evidence="we run on Postgres 15"
    )
    deny = _claim(
        "project",
        "uses",
        "MySQL",
        polarity=False,
        evidence="we are not on MySQL anymore",
    )

    # Act
    affirm_text = _text_for(affirm)
    deny_text = _text_for(deny)

    # Assert
    assert "we run on Postgres 15" in affirm_text
    assert affirm_text.startswith("project uses Postgres")
    assert deny_text.startswith("project not uses MySQL")
    assert "we are not on MySQL anymore" in deny_text
