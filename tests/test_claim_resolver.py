"""Tests for the Qdrant-backed claim entity resolver.

Both dependencies are stubbed at the object level — no live Qdrant /
Ollama. We pin:

* Empty / whitespace input → ``resolve()`` returns ``None`` without
  touching the vector store.
* A search hit at or above ``threshold`` reuses the existing ID; the
  resolver pushes the new surface form into ``aliases``.
* A search hit below ``threshold`` mints a new UUID-shaped ID and
  upserts a point whose payload preserves the original casing as
  ``canonical``.
* Embedder / Qdrant failures are swallowed to ``None`` — the resolver
  never crashes the surrounding extraction.
"""

from __future__ import annotations

from typing import Any

import pytest

from code_memory.claims.resolver import EntityRef, EntityResolver
from code_memory.config import CONFIG
from code_memory.embed.m3 import HybridVec, SparseVec
from code_memory.vector import VectorHit, VectorRecord


# ---------------------------------------------------------------- doubles


class _FakeEmbedder:
    """Deterministic per-text vector so identical inputs match."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_one(self, text: str) -> HybridVec:
        self.calls.append(text)
        # 8-d unit vector seeded by character codepoints — same text
        # always produces the same vector. Cosine between distinct
        # texts will be close to but not exactly 1.
        base = [(ord(c) % 13) / 13.0 for c in text[:8]]
        while len(base) < 8:
            base.append(0.0)
        return HybridVec(dense=base, sparse=SparseVec(indices=[], values=[]))

    def embed(self, texts):  # pragma: no cover - unused in resolver
        return [self.embed_one(t) for t in texts]


class _FakeVector:
    """Stand-in for QdrantStore with explicit search rigging per test."""

    def __init__(self, hits_by_collection: dict[str, list[VectorHit]]) -> None:
        self._hits = hits_by_collection
        self.upserts: list[tuple[str, list[VectorRecord]]] = []
        self.ensured: list[str] = []

    def ensure_collection(self, name: str) -> None:
        self.ensured.append(name)

    def search(
        self,
        collection: str,
        vector: Any,
        top_k: int = 10,
    ) -> list[VectorHit]:
        return list(self._hits.get(collection, []))

    def upsert(self, collection: str, records: Any) -> None:
        self.upserts.append((collection, list(records)))


# ------------------------------------------------------------ fixtures


@pytest.fixture()
def project_slug() -> str:
    return "test-claims"


@pytest.fixture()
def collection_name(project_slug: str) -> str:
    return f"{CONFIG.qdrant_claim_entities}__{project_slug}"


# ---------------------------------------------------------------- tests


def test_resolves_empty_to_none(project_slug: str) -> None:
    # Arrange
    vec = _FakeVector(hits_by_collection={})
    emb = _FakeEmbedder()
    r = EntityResolver(
        project=project_slug, vector=vec, embedder=emb, threshold=0.85
    )

    # Act
    out = r.resolve("   \n\t  ")

    # Assert
    assert out is None
    assert emb.calls == []
    assert vec.upserts == []


def test_reuses_when_score_at_or_above_threshold(
    project_slug: str, collection_name: str
) -> None:
    # Arrange
    existing_hit = VectorHit(
        id="ent-123",
        score=0.91,
        payload={"canonical": "Postgres", "aliases": ["Postgres"]},
    )
    vec = _FakeVector(hits_by_collection={collection_name: [existing_hit]})
    emb = _FakeEmbedder()
    r = EntityResolver(
        project=project_slug, vector=vec, embedder=emb, threshold=0.85
    )

    # Act
    ref = r.resolve("postgres")

    # Assert
    assert isinstance(ref, EntityRef)
    assert ref.id == "ent-123"
    assert ref.canonical == "Postgres"
    assert ref.was_new is False
    # Alias should have been pushed — one upsert (the alias update),
    # no new entity creation.
    assert len(vec.upserts) == 1
    _, recs = vec.upserts[0]
    payload = recs[0].payload
    assert "postgres" in payload["aliases"]
    assert payload["canonical"] == "Postgres"


def test_creates_new_when_below_threshold(
    project_slug: str, collection_name: str
) -> None:
    # Arrange
    weak_hit = VectorHit(
        id="ent-old",
        score=0.42,
        payload={"canonical": "Mongo", "aliases": ["Mongo"]},
    )
    vec = _FakeVector(hits_by_collection={collection_name: [weak_hit]})
    emb = _FakeEmbedder()
    r = EntityResolver(
        project=project_slug, vector=vec, embedder=emb, threshold=0.85
    )

    # Act
    ref = r.resolve("Postgres")

    # Assert
    assert isinstance(ref, EntityRef)
    assert ref.id != "ent-old"
    assert ref.canonical == "Postgres"
    assert ref.was_new is True
    # Exactly one upsert: the new entity. No alias update on the weak hit.
    assert len(vec.upserts) == 1
    _, recs = vec.upserts[0]
    assert recs[0].payload == {
        "canonical": "Postgres",
        "aliases": ["Postgres"],
    }


def test_creates_new_when_no_hits(
    project_slug: str, collection_name: str
) -> None:
    # Arrange
    vec = _FakeVector(hits_by_collection={collection_name: []})
    emb = _FakeEmbedder()
    r = EntityResolver(
        project=project_slug, vector=vec, embedder=emb, threshold=0.85
    )

    # Act
    ref = r.resolve("Redis")

    # Assert
    assert ref is not None
    assert ref.was_new is True
    assert len(vec.upserts) == 1


def test_duplicate_surface_form_does_not_double_register_alias(
    project_slug: str, collection_name: str
) -> None:
    # Arrange — the canonical form is already in aliases
    existing_hit = VectorHit(
        id="ent-9",
        score=0.99,
        payload={"canonical": "Postgres", "aliases": ["Postgres"]},
    )
    vec = _FakeVector(hits_by_collection={collection_name: [existing_hit]})
    emb = _FakeEmbedder()
    r = EntityResolver(
        project=project_slug, vector=vec, embedder=emb, threshold=0.85
    )

    # Act
    ref = r.resolve("Postgres")

    # Assert
    assert ref is not None
    assert ref.was_new is False
    # No alias update because surface form is already present.
    assert vec.upserts == []


def test_embedder_failure_returns_none(project_slug: str) -> None:
    # Arrange
    class _BoomEmbedder:
        def embed_one(self, text: str) -> HybridVec:
            raise RuntimeError("ollama down")

        def embed(self, texts):  # pragma: no cover
            return []

    vec = _FakeVector(hits_by_collection={})
    r = EntityResolver(
        project=project_slug, vector=vec, embedder=_BoomEmbedder()
    )

    # Act
    out = r.resolve("Postgres")

    # Assert
    assert out is None
    assert vec.upserts == []


def test_qdrant_search_failure_falls_through_to_new(
    project_slug: str, collection_name: str
) -> None:
    # Arrange
    class _SearchFails(_FakeVector):
        def search(self, *_args: Any, **_kwargs: Any) -> list[VectorHit]:
            raise LookupError("collection vanished")

    vec = _SearchFails(hits_by_collection={collection_name: []})
    emb = _FakeEmbedder()
    r = EntityResolver(project=project_slug, vector=vec, embedder=emb)

    # Act
    ref = r.resolve("Postgres")

    # Assert — LookupError is the "collection missing" signal; resolver
    # treats it as a miss and mints a new entity.
    assert ref is not None
    assert ref.was_new is True


def test_ensure_collection_called_once(project_slug: str) -> None:
    # Arrange
    vec = _FakeVector(hits_by_collection={})
    emb = _FakeEmbedder()
    r = EntityResolver(project=project_slug, vector=vec, embedder=emb)

    # Act
    r.resolve("Postgres")
    r.resolve("Redis")
    r.resolve("Mongo")

    # Assert — collection is ensured once, not on every resolve()
    assert len(vec.ensured) == 1
