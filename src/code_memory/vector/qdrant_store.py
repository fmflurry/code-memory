from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from ..config import CONFIG
from ..embed import HybridVec

# Vector slot names inside each Qdrant point. Keep stable; collection
# rebuild is required to change them.
DENSE = "dense"
SPARSE = "sparse"


@dataclass
class VectorRecord:
    id: str
    vector: HybridVec
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorHit:
    id: str
    score: float
    payload: dict[str, Any]


class QdrantStore:
    """Hybrid dense + sparse store w/ server-side RRF fusion.

    Collections use Qdrant's named-vector layout: each point carries a
    ``dense`` slot (m3 1024-d cosine) and a ``sparse`` slot (m3 lexical
    weights, IDF-modified). Queries prefetch both, then fuse with
    Reciprocal Rank Fusion so neither view dominates on its own.
    """

    def __init__(
        self,
        url: str | None = None,
        dim: int | None = None,
    ) -> None:
        self.client = QdrantClient(url=url or CONFIG.qdrant_url)
        self.dim = dim or CONFIG.embed_dim

    # --------------------------------------------------------- collection

    def ensure_collection(self, name: str) -> None:
        if self._collection_is_hybrid(name):
            return
        # Either missing or legacy single-vector layout — recreate.
        # Migration note: legacy "code_chunks__<slug>" collections from
        # the dense-only era are NOT auto-migrated. Run a fresh full
        # ingest to repopulate under the hybrid schema.
        self._create_hybrid(name)

    def recreate_collection(self, name: str) -> None:
        """Drop and re-create. Used by full re-ingest."""
        try:
            self.client.delete_collection(collection_name=name)
        except Exception:
            pass
        self._create_hybrid(name)

    def _collection_is_hybrid(self, name: str) -> bool:
        existing = {c.name for c in self.client.get_collections().collections}
        if name not in existing:
            return False
        info = self.client.get_collection(collection_name=name)
        vectors = getattr(info.config.params, "vectors", None)
        sparse = getattr(info.config.params, "sparse_vectors", None)
        has_dense = isinstance(vectors, dict) and DENSE in vectors
        has_sparse = isinstance(sparse, dict) and SPARSE in sparse
        if has_dense and has_sparse:
            return True
        # Legacy layout — caller will drop + recreate.
        try:
            self.client.delete_collection(collection_name=name)
        except Exception:
            pass
        return False

    def _create_hybrid(self, name: str) -> None:
        self.client.create_collection(
            collection_name=name,
            vectors_config={
                DENSE: qm.VectorParams(size=self.dim, distance=qm.Distance.COSINE),
            },
            sparse_vectors_config={
                SPARSE: qm.SparseVectorParams(
                    modifier=qm.Modifier.IDF,
                ),
            },
        )

    # ------------------------------------------------------------- upsert

    def upsert(self, collection: str, records: Iterable[VectorRecord]) -> None:
        points: list[qm.PointStruct] = []
        for r in records:
            vec_payload: dict[str, Any] = {DENSE: r.vector.dense}
            # Skip the sparse slot when the embedder didn't emit one
            # (Ollama backend returns ``HybridVec`` with empty sparse).
            # Qdrant rejects sparse vectors with zero indices.
            if r.vector.sparse.indices:
                vec_payload[SPARSE] = qm.SparseVector(
                    indices=r.vector.sparse.indices,
                    values=r.vector.sparse.values,
                )
            points.append(
                qm.PointStruct(id=r.id, vector=vec_payload, payload=r.payload)
            )
        if not points:
            return
        self.client.upsert(collection_name=collection, points=points)

    # ------------------------------------------------------------- search

    def search(
        self,
        collection: str,
        vector: HybridVec | Sequence[float],
        top_k: int = 10,
        filt: dict[str, Any] | None = None,
        *,
        prefetch_multiplier: int = 4,
        mode: str = "hybrid",
    ) -> list[VectorHit]:
        """Hybrid search with RRF fusion.

        ``vector`` may be a :class:`HybridVec` (preferred) for full
        dense+sparse fusion, or a plain dense sequence for backwards
        compatibility with legacy callers / tests. Sparse-less queries
        degrade gracefully to a dense-only ranking.

        ``prefetch_multiplier`` controls how many candidates each branch
        pulls before fusion. 4x is the Qdrant docs default and gives
        enough overlap for RRF to do useful work.

        ``mode`` is an A/B test seam used by the benchmark harness:
        ``"hybrid"`` (default) fuses both vectors; ``"dense"`` ignores
        the sparse slot entirely. Production callers should leave it at
        the default — query-time degradation is for measurement only.
        """
        qfilter = _to_filter(filt) if filt else None

        # Hybrid mode requires a non-empty sparse query vector. When the
        # embedder is dense-only (Ollama backend), fall through to the
        # dense path so callers don't need to special-case the backend.
        hv = vector if isinstance(vector, HybridVec) else None
        has_sparse = hv is not None and bool(hv.sparse.indices)
        if hv is not None and has_sparse and mode in ("hybrid", "hybrid_dbsf"):
            prefetch = [
                qm.Prefetch(
                    query=hv.dense,
                    using=DENSE,
                    limit=top_k * prefetch_multiplier,
                    filter=qfilter,
                ),
                qm.Prefetch(
                    query=qm.SparseVector(
                        indices=hv.sparse.indices,
                        values=hv.sparse.values,
                    ),
                    using=SPARSE,
                    limit=top_k * prefetch_multiplier,
                    filter=qfilter,
                ),
            ]
            fusion = qm.Fusion.DBSF if mode == "hybrid_dbsf" else qm.Fusion.RRF
            res = self.client.query_points(
                collection_name=collection,
                prefetch=prefetch,
                query=qm.FusionQuery(fusion=fusion),
                limit=top_k,
                with_payload=True,
                query_filter=qfilter,
            )
        else:
            # Dense-only path: legacy callers + benchmark "dense" mode
            dense_vec = vector.dense if isinstance(vector, HybridVec) else list(vector)
            res = self.client.query_points(
                collection_name=collection,
                query=dense_vec,
                using=DENSE,
                limit=top_k,
                query_filter=qfilter,
                with_payload=True,
            )

        return [
            VectorHit(id=str(p.id), score=float(p.score), payload=p.payload or {})
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
