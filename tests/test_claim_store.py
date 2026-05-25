"""Tests for ClaimsStore — bi-temporal contradiction handling and reads.

Uses :func:`tmp_path` so each test gets an isolated SQLite file. We pin:

* Single-valued predicate upsert closes the prior assertion's
  ``valid_to`` when the object changes.
* Multi-valued predicates coexist without closing prior rows.
* ``as_of(when)`` returns the world-state at ``when`` (point-in-time
  bi-temporal query).
* ``current()`` returns only rows with ``valid_to IS NULL``.
* Polarity flip on a single-valued predicate also closes the prior row.
"""

from __future__ import annotations

import time

from code_memory.claims.store import (
    SINGLE_VALUED_PREDICATES,
    ClaimRecord,
    ClaimsStore,
)


def _store(tmp_path) -> ClaimsStore:
    return ClaimsStore(path=tmp_path / "claims.db")


def _claim(
    subject: str,
    predicate: str,
    obj: str,
    *,
    valid_at: float,
    polarity: bool = True,
    head_sha: str | None = "abc123",
) -> ClaimRecord:
    return ClaimRecord(
        subject=subject,
        predicate=predicate,
        object=obj,
        polarity=polarity,
        confidence=0.9,
        evidence_span=f"{subject} {predicate} {obj}",
        valid_at=valid_at,
        head_sha=head_sha,
    )


# ----------------------------------------------------------------- tests


def test_upsert_returns_id(tmp_path) -> None:
    # Arrange
    store = _store(tmp_path)
    claim = _claim("project", "uses", "Qdrant", valid_at=1.0)

    # Act
    cid = store.upsert(claim)

    # Assert
    assert cid == claim.id
    fetched = store.by_id(cid)
    assert fetched is not None
    assert fetched.object == "Qdrant"
    assert fetched.valid_to is None


def test_single_valued_predicate_closes_prior(tmp_path) -> None:
    # Arrange
    assert "uses" in SINGLE_VALUED_PREDICATES
    store = _store(tmp_path)
    store.upsert(_claim("project", "uses", "Postgres", valid_at=10.0))

    # Act — same (subject, predicate), different object → must close prior
    store.upsert(_claim("project", "uses", "MySQL", valid_at=20.0))

    # Assert
    current = store.current()
    assert len(current) == 1
    assert current[0].object == "MySQL"
    # Find the now-closed row
    all_for_subject = store.as_of(15.0, subject="project")
    assert len(all_for_subject) == 1
    assert all_for_subject[0].object == "Postgres"


def test_single_valued_polarity_flip_closes_prior(tmp_path) -> None:
    """'project uses X' followed by 'project does NOT use X' closes prior."""
    # Arrange
    store = _store(tmp_path)
    store.upsert(_claim("project", "uses", "Redis", valid_at=10.0))

    # Act — same s/p/o but polarity flip
    store.upsert(
        _claim("project", "uses", "Redis", valid_at=20.0, polarity=False)
    )

    # Assert
    current = store.current()
    assert len(current) == 1
    assert current[0].polarity is False
    # Prior row should be closed
    closed = store.as_of(15.0)
    assert len(closed) == 1
    assert closed[0].polarity is True


def test_multi_valued_predicate_coexists(tmp_path) -> None:
    # Arrange — 'mentioned' is intentionally not in SINGLE_VALUED_PREDICATES
    assert "mentioned" not in SINGLE_VALUED_PREDICATES
    store = _store(tmp_path)
    store.upsert(_claim("user", "mentioned", "auth-bug", valid_at=10.0))

    # Act
    store.upsert(_claim("user", "mentioned", "perf-bug", valid_at=20.0))

    # Assert — both should remain current
    current = store.current()
    assert len(current) == 2
    assert {c.object for c in current} == {"auth-bug", "perf-bug"}
    assert all(c.valid_to is None for c in current)


def test_as_of_point_in_time_query(tmp_path) -> None:
    # Arrange — sequence: at t=10 uses=A, at t=20 uses=B, at t=30 uses=C
    store = _store(tmp_path)
    store.upsert(_claim("project", "uses", "A", valid_at=10.0))
    store.upsert(_claim("project", "uses", "B", valid_at=20.0))
    store.upsert(_claim("project", "uses", "C", valid_at=30.0))

    # Act — as of t=25 the truth was B
    snapshot = store.as_of(25.0, subject="project")

    # Assert
    objs = {c.object for c in snapshot}
    assert objs == {"B"}


def test_current_filter_by_subject(tmp_path) -> None:
    # Arrange
    store = _store(tmp_path)
    store.upsert(_claim("project", "uses", "Qdrant", valid_at=10.0))
    store.upsert(_claim("user", "prefers", "dark mode", valid_at=10.0))

    # Act
    user_claims = store.current(subject="user")

    # Assert
    assert len(user_claims) == 1
    assert user_claims[0].subject == "user"


def test_count_reflects_all_rows_including_closed(tmp_path) -> None:
    # Arrange
    store = _store(tmp_path)
    store.upsert(_claim("project", "uses", "A", valid_at=10.0))
    store.upsert(_claim("project", "uses", "B", valid_at=20.0))

    # Act
    n = store.count()

    # Assert — both rows persisted, even though one is closed
    assert n == 2


def test_same_object_reupsert_does_not_close_prior(tmp_path) -> None:
    """Restating the same fact should not close the existing assertion."""
    # Arrange
    store = _store(tmp_path)
    store.upsert(_claim("project", "uses", "Qdrant", valid_at=10.0))

    # Act
    store.upsert(_claim("project", "uses", "Qdrant", valid_at=20.0))

    # Assert — both rows remain open; same fact reasserted
    current = store.current()
    assert len(current) == 2
    assert all(c.object == "Qdrant" for c in current)


def test_idempotent_reopen(tmp_path) -> None:
    """A second ClaimsStore on the same file picks up existing data."""
    # Arrange
    path = tmp_path / "claims.db"
    a = ClaimsStore(path=path)
    a.upsert(_claim("user", "prefers", "vim", valid_at=time.time()))
    a.close()

    # Act
    b = ClaimsStore(path=path)

    # Assert
    assert b.count() == 1
    b.close()
