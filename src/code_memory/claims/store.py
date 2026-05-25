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

# Indexes only — schema changes go into _MIGRATIONS too, but for v1 the
# base schema is the full schema. Migrations exist so future columns
# (e.g. embedding refs for entity resolution) can be added without a
# rebuild.
_MIGRATIONS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims(subject)",
    "CREATE INDEX IF NOT EXISTS idx_claims_predicate ON claims(predicate)",
    "CREATE INDEX IF NOT EXISTS idx_claims_valid_at ON claims(valid_at)",
    "CREATE INDEX IF NOT EXISTS idx_claims_valid_to ON claims(valid_to)",
    "CREATE INDEX IF NOT EXISTS idx_claims_head_sha ON claims(head_sha)",
    "CREATE INDEX IF NOT EXISTS idx_claims_session ON claims(session_id)",
)


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

    def upsert(self, claim: ClaimRecord) -> str:
        """Insert a claim, closing any conflicting prior assertion.

        For single-valued predicates: any open ``(subject, predicate)``
        row with a different ``object`` gets ``valid_to`` set to the
        new claim's ``valid_at``. Polarity flips also close.
        """
        if claim.predicate in SINGLE_VALUED_PREDICATES:
            self._close_conflicting(claim)

        self.conn.execute(
            "INSERT INTO claims("
            "id, subject, predicate, object, polarity, confidence, "
            "evidence_span, valid_at, valid_to, recorded_at, "
            "head_sha, session_id, source_prompt_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        self.conn.commit()
        return claim.id

    def upsert_many(self, claims: Iterable[ClaimRecord]) -> list[str]:
        return [self.upsert(c) for c in claims]

    def _close_conflicting(self, claim: ClaimRecord) -> None:
        """Close prior open assertions that conflict with the new claim.

        Conflict := same (subject, predicate) but different object OR
        polarity flip. Scope is global (not per-session) because user
        preferences carry across sessions; restricting to one session
        would defeat the point.
        """
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

    def count(self) -> int:
        (n,) = self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()
        return int(n)

    def close(self) -> None:
        self.conn.close()


_SELECT_ALL = (
    "SELECT id, subject, predicate, object, polarity, confidence, "
    "evidence_span, valid_at, valid_to, recorded_at, "
    "head_sha, session_id, source_prompt_id FROM claims"
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
    )
