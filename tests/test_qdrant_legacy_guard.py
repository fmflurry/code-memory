"""Tests for the legacy-schema guard in QdrantStore.

A bare ``code_chunks`` collection from before the hybrid migration has
no named ``dense`` slot. The old code path raised a cryptic Qdrant
error ("Not existing vector name error") deep inside ``query_points``.
These tests pin the new behavior: ``search`` fails fast with a clear
message and the read path never deletes user data as a side effect.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from code_memory.vector.qdrant_store import DENSE, SPARSE, QdrantStore


# ---------------------------------------------------------------- doubles


class _FakeClient:
    """Stand-in for ``QdrantClient`` that records side effects.

    Only the surface area exercised by the tests is implemented; tests
    that need a different shape can extend on a per-case basis rather
    than ballooning this fake.
    """

    def __init__(self, schema: dict[str, str]) -> None:
        # schema: {collection_name: "hybrid"|"legacy"}
        self._schema = schema
        self.deleted: list[str] = []
        self.queries: list[dict[str, Any]] = []

    def get_collections(self) -> Any:
        return SimpleNamespace(
            collections=[SimpleNamespace(name=n) for n in self._schema]
        )

    def get_collection(self, collection_name: str) -> Any:
        kind = self._schema[collection_name]
        if kind == "hybrid":
            params = SimpleNamespace(
                vectors={DENSE: object()},
                sparse_vectors={SPARSE: object()},
            )
        else:
            params = SimpleNamespace(
                vectors=SimpleNamespace(size=1024, distance="Cosine"),
                sparse_vectors=None,
            )
        return SimpleNamespace(config=SimpleNamespace(params=params))

    def delete_collection(self, collection_name: str) -> None:
        self.deleted.append(collection_name)
        self._schema.pop(collection_name, None)

    def query_points(self, **kwargs: Any) -> Any:
        self.queries.append(kwargs)
        return SimpleNamespace(points=[])


def _store_with(schema: dict[str, str]) -> tuple[QdrantStore, _FakeClient]:
    store = QdrantStore.__new__(QdrantStore)
    fake = _FakeClient(schema)
    store.client = fake  # type: ignore[assignment]
    store.url = "http://localhost:6333"
    store.dim = 1024
    return store, fake


# ---------------------------------------------------------------- inspect


def test_inspect_missing() -> None:
    store, _ = _store_with({})
    assert store._inspect_collection("nope") == "missing"


def test_inspect_legacy() -> None:
    store, _ = _store_with({"code_chunks": "legacy"})
    assert store._inspect_collection("code_chunks") == "legacy"


def test_inspect_hybrid() -> None:
    store, _ = _store_with({"code_chunks__foo": "hybrid"})
    assert store._inspect_collection("code_chunks__foo") == "hybrid"


def test_inspect_is_pure_no_side_effects() -> None:
    store, fake = _store_with({"code_chunks": "legacy"})
    store._inspect_collection("code_chunks")
    store._inspect_collection("code_chunks")
    assert fake.deleted == []  # read path never deletes


# ---------------------------------------------------------------- search guard


def test_search_raises_on_missing_collection() -> None:
    store, fake = _store_with({})
    with pytest.raises(LookupError, match="does not exist"):
        store.search("code_chunks__ghost", [0.1] * 1024)
    assert fake.queries == []
    assert fake.deleted == []


def test_search_raises_on_legacy_with_actionable_message() -> None:
    store, fake = _store_with({"code_chunks": "legacy"})
    with pytest.raises(RuntimeError) as exc:
        store.search("code_chunks", [0.1] * 1024)
    msg = str(exc.value)
    assert "legacy" in msg
    assert "code-memory ingest" in msg
    assert "code_chunks" in msg
    # Critical: the read path must not destroy user data.
    assert fake.deleted == []
    assert fake.queries == []


def test_search_runs_on_hybrid_collection() -> None:
    store, fake = _store_with({"code_chunks__foo": "hybrid"})
    store.search("code_chunks__foo", [0.1] * 1024)
    assert len(fake.queries) == 1
    assert fake.deleted == []


# ---------------------------------------------------------------- ensure


def test_ensure_creates_when_missing() -> None:
    store, fake = _store_with({})
    created: list[str] = []
    store._create_hybrid = lambda n: created.append(n)  # type: ignore[assignment]
    store.ensure_collection("code_chunks__new")
    assert created == ["code_chunks__new"]
    assert fake.deleted == []


def test_ensure_migrates_legacy() -> None:
    store, fake = _store_with({"code_chunks": "legacy"})
    created: list[str] = []
    store._create_hybrid = lambda n: created.append(n)  # type: ignore[assignment]
    store.ensure_collection("code_chunks")
    assert fake.deleted == ["code_chunks"]
    assert created == ["code_chunks"]


def test_ensure_skips_when_hybrid() -> None:
    store, fake = _store_with({"code_chunks__foo": "hybrid"})
    created: list[str] = []
    store._create_hybrid = lambda n: created.append(n)  # type: ignore[assignment]
    store.ensure_collection("code_chunks__foo")
    assert created == []
    assert fake.deleted == []
