"""SQLite store for extracted user claims with bi-temporal validity.

Schema mirrors the ``episodes`` table's idempotent-migration pattern.
Each claim row carries:

  * ``valid_at``    — when the user asserted it (prompt timestamp)
  * ``valid_to``    — when the assertion was superseded (NULL = current)
  * ``recorded_at`` — when we ingested it (system clock)
  * ``head_sha``    — git HEAD at extraction time

The combination gives bi-temporal queries: "as of commit X, what did the
user say about Y?" and "what was the user's stated preference for Y at
time T according to what we knew at time T'?".

Contradiction handling: predicates listed in :data:`SINGLE_VALUED_PREDICATES`
are treated as functional — at most one open ``(subject, predicate)`` per
session. Upserting a new ``(s, p, o2)`` closes the prior ``(s, p, o1)`` by
setting its ``valid_to = new.valid_at``.

Multi-valued predicates (e.g. ``mentioned``, ``worked-on``) coexist
without conflict.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..config import CONFIG

# Predicates whose object is functional: at most one currently-valid
# assertion per (subject, predicate). A second extraction with a
# different object closes the previous one.
#
# Keep this list small and hand-curated — automatic detection would
# require a richer schema than we want to ask the LLM to produce.
SINGLE_VALUED_PREDICATES: frozenset[str] = frozenset(
    {
        "prefers",
        "uses",  # primary-tool sense: "we use Postgres" → switching closes prior
        "deployed-to",
        "is-located-at",
        "is-a",
        "owns",
        "assigned-to",
        "depends-on",
    }
)


_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    polarity INTEGER NOT NULL,
    confidence REAL NOT NULL,
    evidence_span TEXT NOT NULL,
    valid_at REAL NOT NULL,
    valid_to REAL,
    recorded_at REAL NOT NULL,
    head_sha TEXT,
    session_id TEXT,
    source_prompt_id TEXT
);
"""

# Migrations are idempotent. Each statement runs independently; failures
# from a re-run (duplicate column, already-existing index) are swallowed
# so an existing DB catches up to the latest schema on open. Columns
# referenced by an index MUST appear in the migration list before the
# index that uses them.
_MIGRATIONS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims(subject)",
    "CREATE INDEX IF NOT EXISTS idx_claims_predicate ON claims(predicate)",
    "CREATE INDEX IF NOT EXISTS idx_claims_valid_at ON claims(valid_at)",
    "CREATE INDEX IF NOT EXISTS idx_claims_valid_to ON claims(valid_to)",
    "CREATE INDEX IF NOT EXISTS idx_claims_head_sha ON claims(head_sha)",
    "CREATE INDEX IF NOT EXISTS idx_claims_session ON claims(session_id)",
    # Entity-resolution back-references: nullable so legacy rows that
    # were extracted before the resolver shipped keep working.
    "ALTER TABLE claims ADD COLUMN entity_subject_id TEXT",
    "ALTER TABLE claims ADD COLUMN entity_object_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_claims_entity_subject ON claims(entity_subject_id)",
    "CREATE INDEX IF NOT EXISTS idx_claims_entity_object ON claims(entity_object_id)",
)


class UpsertResult(str):
    """Result of :meth:`ClaimsStore.upsert`.

    Subclasses ``str`` so the old contract — ``upsert`` returning a
    claim id — is preserved for callers that just want the id:

        cid = store.upsert(record)   # works as before
        assert isinstance(cid, str)  # still true

    Newer callers (the Qdrant claim indexer) read the extra fields to
    know which prior rows were closed in the same transaction so they
    can flip ``open=false`` on the matching Qdrant points.
    """

    closed_ids: list[str]
    was_new: bool

    def __new__(
        cls,
        claim_id: str,
        *,
        closed_ids: list[str] | None = None,
        was_new: bool = True,
    ) -> "UpsertResult":
        inst = super().__new__(cls, claim_id)
        inst.closed_ids = list(closed_ids or [])
        inst.was_new = was_new
        return inst

    @property
    def claim_id(self) -> str:
        return str(self)


@dataclass
class ClaimRecord:
    subject: str
    predicate: str
    object: str
    polarity: bool = True
    confidence: float = 1.0
    evidence_span: str = ""
    valid_at: float = field(default_factory=time.time)
    valid_to: float | None = None
    recorded_at: float = field(default_factory=time.time)
    head_sha: str | None = None
    session_id: str | None = None
    source_prompt_id: str | None = None
    # Canonical entity IDs from the Qdrant entity resolver. NULL when
    # resolution was skipped (claims_enabled but resolver disabled, or
    # legacy rows from before the resolver shipped).
    entity_subject_id: str | None = None
    entity_object_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


class ClaimsStore:
    """SQLite-backed claim store with single-valued predicate contradiction."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CONFIG.claims_db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(_BASE_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                # idempotent migration — already applied
                pass
        self.conn.commit()

    # -------------------------------------------------------------- write

    def upsert(self, claim: ClaimRecord) -> UpsertResult:
        """Insert a claim, closing any conflicting prior assertion.

        Dedupe: an open row with the same (subject, predicate, object,
        polarity) is refreshed in place — its ``recorded_at`` becomes
        ``now``, its ``confidence`` becomes ``max(prev, new)``, and a
        non-empty new ``evidence_span`` overwrites a missing/equal one.
        This prevents bloat when the agent re-asserts the same claim
        across turns or sessions (which happens often with the
        "ACT BEFORE ANSWERING" nudge in the plugins).

        For single-valued predicates: any open ``(subject, predicate)``
        row with a different ``object`` gets ``valid_to`` set to the
        new claim's ``valid_at``. Polarity flips also close.

        Returns an :class:`UpsertResult` carrying both the canonical id
        of the (refreshed-or-new) claim AND the ids of any rows that got
        closed by this insertion. Callers maintaining a secondary index
        (the Qdrant claim vector store) need both: insert/refresh the
        canonical id, and flip ``open=false`` on the closed ids.

        Backwards compatibility: ``UpsertResult`` is a string subclass
        so legacy callers using ``store.upsert(c)`` as the claim id keep
        working (``str(result) == result.claim_id``).
        """
        closed_ids: list[str] = []
        if claim.predicate in SINGLE_VALUED_PREDICATES:
            closed_ids = self._close_conflicting(claim)

        existing_id = self._find_open_duplicate(claim)
        if existing_id is not None:
            self._refresh_existing(existing_id, claim)
            self.conn.commit()
            return UpsertResult(existing_id, closed_ids=closed_ids, was_new=False)

        self.conn.execute(
            "INSERT INTO claims("
            "id, subject, predicate, object, polarity, confidence, "
            "evidence_span, valid_at, valid_to, recorded_at, "
            "head_sha, session_id, source_prompt_id, "
            "entity_subject_id, entity_object_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim.id,
                claim.subject,
                claim.predicate,
                claim.object,
                1 if claim.polarity else 0,
                claim.confidence,
                claim.evidence_span,
                claim.valid_at,
                claim.valid_to,
                claim.recorded_at,
                claim.head_sha,
                claim.session_id,
                claim.source_prompt_id,
                claim.entity_subject_id,
                claim.entity_object_id,
            ),
        )
        self.conn.commit()
        return UpsertResult(claim.id, closed_ids=closed_ids, was_new=True)

    def _find_open_duplicate(self, claim: ClaimRecord) -> str | None:
        """Return the id of an open row identical on (s,p,o,polarity), or None."""
        row = self.conn.execute(
            "SELECT id FROM claims "
            "WHERE valid_to IS NULL "
            "  AND subject = ? AND predicate = ? AND object = ? "
            "  AND polarity = ? "
            "LIMIT 1",
            (
                claim.subject,
                claim.predicate,
                claim.object,
                1 if claim.polarity else 0,
            ),
        ).fetchone()
        return None if row is None else str(row[0])

    def _refresh_existing(self, claim_id: str, claim: ClaimRecord) -> None:
        """Refresh an existing open dupe with the new claim's metadata.

        Confidence is monotonic non-decreasing (keep the strongest
        assertion seen). Evidence is overwritten only when the new
        span is non-empty so we never erase a quote with a blank one.
        Session/prompt-id only fill in if previously NULL, preserving
        the first observation's provenance.
        """
        self.conn.execute(
            "UPDATE claims SET "
            "  confidence = MAX(confidence, ?), "
            "  evidence_span = CASE "
            "    WHEN ? <> '' THEN ? "
            "    ELSE evidence_span "
            "  END, "
            "  recorded_at = ?, "
            "  head_sha = COALESCE(?, head_sha), "
            "  session_id = COALESCE(session_id, ?), "
            "  source_prompt_id = COALESCE(source_prompt_id, ?), "
            "  entity_subject_id = COALESCE(entity_subject_id, ?), "
            "  entity_object_id = COALESCE(entity_object_id, ?) "
            "WHERE id = ?",
            (
                claim.confidence,
                claim.evidence_span,
                claim.evidence_span,
                claim.recorded_at,
                claim.head_sha,
                claim.session_id,
                claim.source_prompt_id,
                claim.entity_subject_id,
                claim.entity_object_id,
                claim_id,
            ),
        )

    def upsert_many(self, claims: Iterable[ClaimRecord]) -> list[UpsertResult]:
        return [self.upsert(c) for c in claims]

    def _close_conflicting(self, claim: ClaimRecord) -> list[str]:
        """Close prior open assertions that conflict with the new claim.

        Conflict := same (subject, predicate) but different object OR
        polarity flip. Scope is global (not per-session) because user
        preferences carry across sessions; restricting to one session
        would defeat the point.

        Returns the ids of the rows whose ``valid_to`` was just set, so
        a caller maintaining a secondary index can flip their ``open``
        flag in lockstep. Returns ``[]`` when nothing matched.
        """
        rows = self.conn.execute(
            "SELECT id FROM claims "
            "WHERE subject = ? AND predicate = ? "
            "  AND valid_to IS NULL "
            "  AND (object <> ? OR polarity <> ?)",
            (
                claim.subject,
                claim.predicate,
                claim.object,
                1 if claim.polarity else 0,
            ),
        ).fetchall()
        closed_ids = [str(r[0]) for r in rows]
        if closed_ids:
            self.conn.execute(
                "UPDATE claims "
                "SET valid_to = ? "
                "WHERE subject = ? AND predicate = ? "
                "  AND valid_to IS NULL "
                "  AND (object <> ? OR polarity <> ?)",
                (
                    claim.valid_at,
                    claim.subject,
                    claim.predicate,
                    claim.object,
                    1 if claim.polarity else 0,
                ),
            )
        return closed_ids

    # --------------------------------------------------------------- read

    def current(self, subject: str | None = None) -> list[ClaimRecord]:
        """Return all currently-valid claims (``valid_to IS NULL``)."""
        if subject is None:
            rows = self.conn.execute(
                _SELECT_ALL + " WHERE valid_to IS NULL ORDER BY valid_at DESC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                _SELECT_ALL
                + " WHERE valid_to IS NULL AND subject = ? "
                "ORDER BY valid_at DESC",
                (subject,),
            ).fetchall()
        return [_row_to_claim(r) for r in rows]

    def as_of(self, when: float, subject: str | None = None) -> list[ClaimRecord]:
        """Bi-temporal point query: claims valid at world-time ``when``.

        A claim is valid at ``when`` iff
        ``valid_at <= when < (valid_to or +inf)``.
        """
        base = (
            _SELECT_ALL
            + " WHERE valid_at <= ? AND (valid_to IS NULL OR valid_to > ?)"
        )
        if subject is None:
            rows = self.conn.execute(
                base + " ORDER BY valid_at DESC", (when, when)
            ).fetchall()
        else:
            rows = self.conn.execute(
                base + " AND subject = ? ORDER BY valid_at DESC",
                (when, when, subject),
            ).fetchall()
        return [_row_to_claim(r) for r in rows]

    def by_id(self, claim_id: str) -> ClaimRecord | None:
        row = self.conn.execute(
            _SELECT_ALL + " WHERE id = ?", (claim_id,)
        ).fetchone()
        return _row_to_claim(row) if row else None

    def by_ids(self, ids: list[str]) -> list[ClaimRecord]:
        """Batch fetch by id list. Preserves caller ordering.

        Mirrors :meth:`EpisodicStore.by_ids`. Used to hydrate
        ``ClaimRecord`` rows after a Qdrant semantic search returns ids.
        Unknown ids are silently dropped — they may have been pruned
        between the vector hit and this lookup.
        """
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            _SELECT_ALL + f" WHERE id IN ({placeholders})", ids
        ).fetchall()
        by_id = {row[0]: row for row in rows}
        return [_row_to_claim(by_id[i]) for i in ids if i in by_id]

    def count(self) -> int:
        (n,) = self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()
        return int(n)

    def close(self) -> None:
        self.conn.close()


_SELECT_ALL = (
    "SELECT id, subject, predicate, object, polarity, confidence, "
    "evidence_span, valid_at, valid_to, recorded_at, "
    "head_sha, session_id, source_prompt_id, "
    "entity_subject_id, entity_object_id FROM claims"
)


def _row_to_claim(row: tuple[Any, ...]) -> ClaimRecord:
    return ClaimRecord(
        id=row[0],
        subject=row[1],
        predicate=row[2],
        object=row[3],
        polarity=bool(row[4]),
        confidence=row[5],
        evidence_span=row[6],
        valid_at=row[7],
        valid_to=row[8],
        recorded_at=row[9],
        head_sha=row[10],
        session_id=row[11],
        source_prompt_id=row[12],
        entity_subject_id=row[13] if len(row) > 13 else None,
        entity_object_id=row[14] if len(row) > 14 else None,
    )
