from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from ..config import CONFIG


@dataclass
class VectorRecord:
    id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorHit:
    id: str
    score: float
    payload: dict[str, Any]


class QdrantStore:
    def __init__(
        self,
        url: str | None = None,
        dim: int | None = None,
    ) -> None:
        self.client = QdrantClient(url=url or CONFIG.qdrant_url)
        self.dim = dim or CONFIG.embed_dim

    def ensure_collection(self, name: str) -> None:
        existing = {c.name for c in self.client.get_collections().collections}
        if name in existing:
            return
        self.client.create_collection(
            collection_name=name,
            vectors_config=qm.VectorParams(size=self.dim, distance=qm.Distance.COSINE),
        )

    def upsert(self, collection: str, records: Iterable[VectorRecord]) -> None:
        points = [
            qm.PointStruct(id=r.id, vector=r.vector, payload=r.payload)
            for r in records
        ]
        if not points:
            return
        self.client.upsert(collection_name=collection, points=points)

    def search(
        self,
        collection: str,
        vector: Sequence[float],
        top_k: int = 10,
        filt: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        qfilter = _to_filter(filt) if filt else None
        res = self.client.query_points(
            collection_name=collection,
            query=list(vector),
            limit=top_k,
            query_filter=qfilter,
            with_payload=True,
        )
        return [
            VectorHit(id=str(p.id), score=p.score, payload=p.payload or {})
            for p in res.points
        ]

    def delete_by_path(self, collection: str, path: str) -> None:
        self.client.delete(
            collection_name=collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[qm.FieldCondition(key="path", match=qm.MatchValue(value=path))]
                )
            ),
        )


def _to_filter(filt: dict[str, Any]) -> qm.Filter:
    must = [
        qm.FieldCondition(key=k, match=qm.MatchValue(value=v))
        for k, v in filt.items()
    ]
    return qm.Filter(must=must)
