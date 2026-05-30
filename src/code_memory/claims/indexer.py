"""Qdrant-backed semantic index over user claims.

``ClaimsStore`` (SQLite) is the source of truth for the bi-temporal
claim history. This module layers a vector index on top so retrieval
can match claims semantically — "we use Postgres" surfaces for a query
about "DB choice" — instead of relying on the token-overlap heuristic
in :func:`code_memory.orchestrator.retrieve._rank_claims`.

Design choices:

* **Keep + flag, not delete.** When a claim is superseded
  (``valid_to`` set), the Qdrant point stays and gets
  ``payload.open = False``. Default retrieval filters ``open=true``;
  this keeps the door open for bi-temporal ``as_of`` semantic queries
  later without re-embedding the corpus.
* **Embed triple + evidence.** The evidence span carries the user's
  raw phrasing, which is where synonym recall lives ("DB" vs
  "Postgres"). The triple alone is too terse to embed well.
* **Lazy backfill.** First access detects ``len(qdrant_claims) == 0``
  while ``claims.db`` is non-empty and re-embeds every row. Idempotent:
  re-runs are cheap because the embedder caches per-text.
* **Token-overlap fallback.** If the embedder or Qdrant is unavailable
  the caller falls back to ``_rank_claims`` (see ``retrieve.py``). The
  indexer raises only when the operation is fundamentally
  side-effecting (upsert), not when reads fail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..config import CONFIG, Config, detect_project_slug
from ..embed import Embedder, HybridVec, get_embedder
from ..vector import QdrantStore, VectorHit, VectorRecord
from .store import ClaimRecord, ClaimsStore, UpsertResult

_LOG = logging.getLogger(__name__)


@dataclass
class ClaimsIndexer:
    """SQLite + Qdrant facade for claim writes and semantic reads.

    Holds references to the persistent components; callers re-use one
    indexer per project across many upserts. Not thread-safe — the
    underlying SQLite connection isn't either.
    """

    store: ClaimsStore
    vector: QdrantStore
    embedder: Embedder
    collection: str
    # Cosine threshold for semantic near-duplicate collapse. Default
    # pulls from the global config so tests can inject a fake value
    # without monkey-patching CONFIG.
    semantic_dedup_threshold: float = field(
        default_factory=lambda: CONFIG.claims_semantic_dedup_threshold
    )
    _backfilled: bool = False

    # ------------------------------------------------------------- write

    def upsert(self, claim: ClaimRecord) -> UpsertResult:
        """Persist ``claim`` to SQLite + Qdrant atomically (SQLite first).

        SQLite write is authoritative — if Qdrant raises after the
        SQLite commit, the row still lands and a later
        :meth:`ensure_backfilled` call (or the next ``retrieve``) will
        re-embed it. We prefer "Qdrant temporarily behind" over "lose
        the claim entirely."

        Semantic near-duplicate collapse runs BEFORE the SQLite write.
        When the new claim's embedding sits within
        ``semantic_dedup_threshold`` cosine of an existing open claim,
        the existing row is refreshed in place (recorded_at, confidence,
        evidence) and no new row is inserted. Exact-triple matches still
        flow through ``store.upsert`` and hit the SQL-level dedupe so
        single-valued-predicate conflict handling remains intact.
        """
        self.ensure_backfilled()

        near_id = self._find_near_duplicate(claim)
        if near_id is not None:
            self.store.refresh_existing(near_id, claim)
            existing = self.store.by_id(near_id)
            if existing is not None:
                self.vector.set_payload(
                    self.collection,
                    [near_id],
                    _payload_for(existing, open_=True),
                )
            _LOG.debug(
                "claims indexer: semantic-dedup collapsed new claim into %s",
                near_id,
            )
            return UpsertResult(near_id, closed_ids=[], was_new=False)

        result = self.store.upsert(claim)

        # Close path: any predecessor rows closed by this insert get
        # their ``open`` payload flipped. Cheap: no re-embed.
        if result.closed_ids:
            self.vector.set_payload(
                self.collection,
                result.closed_ids,
                {"open": False},
            )

        if result.was_new:
            self._embed_and_upsert(result.claim_id, claim)
        else:
            # Existing-row refresh: triple unchanged, but confidence and
            # ``recorded_at`` may have moved. Update payload so rerank
            # sees the new score without paying for an embed.
            self.vector.set_payload(
                self.collection,
                [result.claim_id],
                _payload_for(claim, open_=True),
            )
        return result

    def upsert_many(self, claims: Sequence[ClaimRecord]) -> list[UpsertResult]:
        return [self.upsert(c) for c in claims]

    # -------------------------------------------------------------- read

    def search(
        self,
        query_vec: HybridVec,
        top_k: int = 5,
        *,
        include_closed: bool = False,
    ) -> list[VectorHit]:
        """Semantic top-k over claim points.

        Default filter is ``open=true`` so superseded claims don't leak
        into the orientation context. ``include_closed`` opens the door
        for future bi-temporal point-in-time queries (see the design
        doc note in :mod:`code_memory.orchestrator.retrieve`).

        Returns an empty list (not an error) when the collection is
        missing — that's the "claims-disabled project" path and the
        caller should fall back to token-overlap silently.
        """
        if self.vector._inspect_collection(self.collection) == "missing":
            return []
        filt: dict[str, Any] | None = None if include_closed else {"open": True}
        try:
            return self.vector.search(
                self.collection,
                query_vec,
                top_k=top_k,
                filt=filt,
                mode="dense",
            )
        except Exception:  # noqa: BLE001
            # Vector backend hiccup — return empty so the orchestrator
            # falls through to the SQLite token-overlap fallback rather
            # than dropping claims from the context pack entirely.
            return []

    # --------------------------------------------------------- backfill

    def ensure_backfilled(self) -> int:
        """Embed every claim row missing from Qdrant. Idempotent.

        Runs once per indexer instance. Re-creates the collection if it
        was missing. Returns the count of rows embedded (``0`` when
        already in sync). Cheap on warm runs — the embedder cache hits
        for previously-seen triples.

        We compare row counts as a soft sync check, not point IDs. If
        SQLite has 42 rows and Qdrant has 42 points we trust they're
        the same set; drift detection would need a per-id scan and we
        don't currently need it.
        """
        if self._backfilled:
            return 0
        self.vector.ensure_collection(self.collection)
        sqlite_count = self.store.count()
        if sqlite_count == 0:
            self._backfilled = True
            return 0
        qdrant_count = self.vector.count(self.collection)
        if qdrant_count >= sqlite_count:
            self._backfilled = True
            return 0
        # Backfill all rows (open + closed) so bi-temporal queries work
        # later. ``current()`` returns only open rows, so use a wider
        # accessor.
        rows = self._all_rows()
        records: list[VectorRecord] = []
        for claim in rows:
            hv = self.embedder.embed_one(_text_for(claim))
            records.append(
                VectorRecord(
                    id=claim.id,
                    vector=hv,
                    payload=_payload_for(claim, open_=claim.valid_to is None),
                )
            )
        if records:
            self.vector.upsert(self.collection, records)
        self._backfilled = True
        return len(records)

    # ------------------------------------------------------------ helpers

    def _find_near_duplicate(self, claim: ClaimRecord) -> str | None:
        """Return the id of an open claim within cosine threshold, else None.

        Reasons we return ``None`` (and let the SQLite store handle the
        write):

        * Threshold ``>= 1.0`` — feature disabled.
        * Collection missing — no points to compare against yet.
        * Embedder or vector backend raises — semantic dedup is an
          optimization, never a hard dependency.
        * Top hit's score below threshold.
        * Top hit's stored triple matches the new claim exactly — the
          SQL-level dedupe in :meth:`ClaimsStore.upsert` already handles
          that case (and preserves single-valued-predicate semantics).
        """
        if self.semantic_dedup_threshold >= 1.0:
            return None
        if self.vector._inspect_collection(self.collection) == "missing":
            return None
        try:
            hv = self.embedder.embed_one(_text_for(claim))
            hits = self.vector.search(
                self.collection,
                hv,
                top_k=1,
                filt={"open": True, "polarity": claim.polarity},
                mode="dense",
            )
        except Exception:  # noqa: BLE001
            _LOG.debug("claims indexer: semantic dedup search failed", exc_info=True)
            return None
        if not hits:
            return None
        top = hits[0]
        if top.score < self.semantic_dedup_threshold:
            return None
        if (
            top.payload.get("subject") == claim.subject
            and top.payload.get("predicate") == claim.predicate
            and top.payload.get("object") == claim.object
        ):
            return None
        return str(top.id)

    def _embed_and_upsert(self, claim_id: str, claim: ClaimRecord) -> None:
        hv = self.embedder.embed_one(_text_for(claim))
        self.vector.upsert(
            self.collection,
            [
                VectorRecord(
                    id=claim_id,
                    vector=hv,
                    payload=_payload_for(claim, open_=True),
                )
            ],
        )

    def _all_rows(self) -> list[ClaimRecord]:
        """Every row, open or closed. Used for backfill only."""
        rows = self.store.conn.execute(
            "SELECT id, subject, predicate, object, polarity, confidence, "
            "evidence_span, valid_at, valid_to, recorded_at, "
            "head_sha, session_id, source_prompt_id, "
            "entity_subject_id, entity_object_id FROM claims"
        ).fetchall()
        # Reuse the row->record decoder.
        from .store import _row_to_claim
        return [_row_to_claim(r) for r in rows]


def _text_for(claim: ClaimRecord) -> str:
    """Composite text used as the embed input for a claim.

    ``subject {predicate} object`` is the canonical triple. The
    evidence span — the verbatim user phrasing — gets appended so the
    embedder also sees the natural-language vocabulary the user used
    when asserting the claim. That's where synonym recall comes from
    (e.g. "DB" in evidence aligns with "Postgres" in object).
    """
    polarity = "" if claim.polarity else "not "
    head = f"{claim.subject} {polarity}{claim.predicate} {claim.object}".strip()
    if claim.evidence_span:
        return f"{head}\n\n{claim.evidence_span}"
    return head


def _payload_for(claim: ClaimRecord, *, open_: bool) -> dict[str, Any]:
    """Payload stored alongside each Qdrant point.

    Carries just enough metadata for reranking (confidence, recency
    via valid_at) and filtering (open). Anything else stays in SQLite.
    """
    return {
        "open": open_,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "object": claim.object,
        "polarity": claim.polarity,
        "confidence": claim.confidence,
        "valid_at": claim.valid_at,
        "head_sha": claim.head_sha,
    }


def make_claims_indexer(
    project: str | None = None,
    *,
    cfg: Config | None = None,
    embedder: Embedder | None = None,
    vector: QdrantStore | None = None,
    store: ClaimsStore | None = None,
) -> ClaimsIndexer:
    """Construct a fully wired :class:`ClaimsIndexer` for ``project``.

    All deps are optional so tests can inject fakes. Production callers
    typically pass nothing and get the configured embedder + Qdrant
    client + per-project SQLite path.
    """
    slug = project or detect_project_slug()
    config = cfg or CONFIG.for_project(slug)
    return ClaimsIndexer(
        store=store or ClaimsStore(path=config.claims_db),
        vector=vector or QdrantStore(),
        embedder=embedder or get_embedder(),
        collection=config.qdrant_claims,
    )
