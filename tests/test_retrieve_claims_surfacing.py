"""Tests for claim surfacing inside the retrieve pack.

We exercise the lightweight token-overlap ranker directly so the test
suite stays fast and doesn't need Qdrant / Ollama running. The ranker
contract:

* No query tokens → fall back to recency * confidence ordering.
* Some overlap → claims that share more tokens with the query rank
  higher.
* Older claims decay (half-life = 30 days), but a very recent low-overlap
  claim still trails a high-overlap older one.
* ``ContextPack.to_dict()`` and ``.render()`` include claims when
  present.
"""

from __future__ import annotations

import time

from code_memory.claims import ClaimRecord
from code_memory.orchestrator.retrieve import (
    ContextPack,
    _rank_claims,
)


def _claim(
    subject: str,
    predicate: str,
    obj: str,
    *,
    confidence: float = 0.9,
    valid_at: float | None = None,
    polarity: bool = True,
) -> ClaimRecord:
    return ClaimRecord(
        subject=subject,
        predicate=predicate,
        object=obj,
        polarity=polarity,
        confidence=confidence,
        evidence_span=f"{subject} {predicate} {obj}",
        valid_at=valid_at if valid_at is not None else time.time(),
    )


def test_rank_prefers_token_overlap() -> None:
    """Claims sharing tokens with the query rank ahead of unrelated ones."""
    # Arrange
    claims = [
        _claim("project", "uses", "Postgres"),
        _claim("project", "uses", "FalkorDB"),
        _claim("user", "prefers", "dark mode"),
    ]

    # Act — query that literally mentions one of the objects
    ranked = _rank_claims("how is Postgres configured?", claims)

    # Assert
    assert len(ranked) == 1, f"unexpected matches: {[c.object for c in ranked]}"
    assert ranked[0].object == "Postgres"


def test_rank_falls_back_to_confidence_when_no_tokens() -> None:
    # Arrange
    claims = [
        _claim("a", "b", "low", confidence=0.6),
        _claim("a", "b", "high", confidence=0.95),
    ]

    # Act — query of pure punctuation produces no tokens
    ranked = _rank_claims("??? !!!", claims)

    # Assert
    assert ranked[0].object == "high"


def test_rank_recency_breaks_overlap_ties() -> None:
    # Arrange
    now = time.time()
    old = _claim(
        "project", "uses", "Qdrant", valid_at=now - 90 * 24 * 3600
    )  # 90 days old
    fresh = _claim("project", "uses", "Qdrant", valid_at=now - 60)

    # Act
    ranked = _rank_claims("Qdrant config", [old, fresh])

    # Assert
    assert ranked[0].valid_at == fresh.valid_at


def test_rank_drops_zero_overlap_when_tokens_present() -> None:
    # Arrange — a query with real tokens and a claim sharing none of them
    claims = [_claim("user", "prefers", "vim")]

    # Act
    ranked = _rank_claims("Postgres migration plan", claims)

    # Assert — no overlap means the claim is dropped when ranking by tokens
    assert ranked == []


def test_context_pack_render_includes_claims() -> None:
    # Arrange
    pack = ContextPack(
        query="what db do we use?",
        claims=[_claim("project", "uses", "Postgres")],
    )

    # Act
    text = pack.render()

    # Assert
    assert "## User claims" in text
    assert "project uses Postgres" in text


def test_context_pack_to_dict_includes_claims() -> None:
    # Arrange
    pack = ContextPack(
        query="q",
        claims=[
            _claim("project", "uses", "Postgres"),
            _claim("project", "uses", "Redis", polarity=False),
        ],
    )

    # Act
    payload = pack.to_dict()

    # Assert
    assert "claims" in payload
    assert len(payload["claims"]) == 2
    negated = next(c for c in payload["claims"] if c["object"] == "Redis")
    assert negated["polarity"] is False


def test_context_pack_render_omits_claims_section_when_empty() -> None:
    # Arrange
    pack = ContextPack(query="q")

    # Act
    text = pack.render()

    # Assert
    assert "## User claims" not in text
