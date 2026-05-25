"""Qdrant-backed entity resolution for claim subjects and objects.

Each unique entity referenced by a claim becomes a point in a per-project
``claim_entities__<slug>`` collection. The point payload stores:

  * ``canonical`` — the first form we saw for this entity (preserved
    casing).
  * ``aliases``  — every distinct form we've seen since.

On :meth:`EntityResolver.resolve`:

  1. Embed the input text via the project's :mod:`Ollama` embedder.
  2. Search the collection. If the top hit's cosine score is
     ``>= threshold`` (default ``0.85``), reuse it: append the new
     surface form to ``aliases`` and return the existing ID.
  3. Otherwise create a fresh point with a new UUID.

Concurrency caveat: extraction may run in detached background processes,
so two near-simultaneous extractions of the same entity could each take
the "create new" branch. That's acceptable — the next extraction sees
both and merges around whichever wins the search. False merges (two
distinct entities collapsed into one) are the failure mode worth fearing,
hence the conservative threshold.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from ..config import CONFIG, Config
from ..embed import Embedder, get_embedder
from ..vector import QdrantStore, VectorRecord

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntityRef:
    """Return type for a single resolve() call."""

    id: str
    canonical: str
    was_new: bool


class EntityResolver:
    """Embed → search → reuse-or-create entity points in Qdrant."""

    def __init__(
        self,
        project: str,
        vector: QdrantStore | None = None,
        embedder: Embedder | None = None,
        threshold: float | None = None,
        cfg: Config | None = None,
    ) -> None:
        self.cfg = cfg or CONFIG.for_project(project)
        self.threshold = (
            threshold if threshold is not None else CONFIG.claims_entity_threshold
        )
        self.vector = vector or QdrantStore()
        self.embedder = embedder or get_embedder()
        self._collection = self.cfg.qdrant_claim_entities
        self._ensured = False

    # ------------------------------------------------------------ public

    def resolve(self, text: str) -> EntityRef | None:
        """Resolve a surface form to a canonical entity.

        Returns ``None`` when the input is empty after stripping, or when
        the embedder / Qdrant client fails — the caller should treat
        ``None`` as "skip entity resolution for this row" and persist the
        claim without an entity ID.
        """
        surface = (text or "").strip()
        if not surface:
            return None

        try:
            self._ensure_collection()
        except Exception:  # noqa: BLE001
            _LOG.exception("entity resolver: ensure_collection failed")
            return None

        try:
            vec = self.embedder.embed_one(surface)
        except Exception:  # noqa: BLE001
            _LOG.exception("entity resolver: embed failed for %r", surface)
            return None

        try:
            hits = self.vector.search(self._collection, vec, top_k=1)
        except LookupError:
            # Collection vanished between ensure_collection and search.
            # Treat as miss and continue with a new entity.
            hits = []
        except Exception:  # noqa: BLE001
            _LOG.exception("entity resolver: search failed for %r", surface)
            return None

        if hits and hits[0].score >= self.threshold:
            top = hits[0]
            canonical = str(top.payload.get("canonical") or surface)
            self._record_alias(top.id, surface, top.payload)
            return EntityRef(id=top.id, canonical=canonical, was_new=False)

        # No close match — mint a new entity.
        new_id = str(uuid.uuid4())
        try:
            self.vector.upsert(
                self._collection,
                [
                    VectorRecord(
                        id=new_id,
                        vector=vec,
                        payload={"canonical": surface, "aliases": [surface]},
                    )
                ],
            )
        except Exception:  # noqa: BLE001
            _LOG.exception(
                "entity resolver: upsert failed for new entity %r", surface
            )
            return None
        return EntityRef(id=new_id, canonical=surface, was_new=True)

    # ----------------------------------------------------------- helpers

    def _ensure_collection(self) -> None:
        if self._ensured:
            return
        self.vector.ensure_collection(self._collection)
        self._ensured = True

    def _record_alias(
        self,
        entity_id: str,
        surface: str,
        payload: dict[str, Any],
    ) -> None:
        """Append ``surface`` to the entity's alias list if it's new.

        Best-effort: failures here only mean the alias list lags slightly
        behind reality. The canonical ID still points at the right
        entity.
        """
        existing = payload.get("aliases")
        aliases: list[str] = (
            list(existing) if isinstance(existing, list) else []
        )
        if surface in aliases:
            return
        aliases.append(surface)
        # We don't have the original embedding here, so re-embed the
        # canonical form to keep the point's vector stable.
        canonical = str(payload.get("canonical") or surface)
        try:
            vec = self.embedder.embed_one(canonical)
        except Exception:  # noqa: BLE001
            _LOG.exception(
                "entity resolver: re-embed for alias update failed (%r)",
                canonical,
            )
            return
        try:
            self.vector.upsert(
                self._collection,
                [
                    VectorRecord(
                        id=entity_id,
                        vector=vec,
                        payload={
                            "canonical": canonical,
                            "aliases": aliases,
                        },
                    )
                ],
            )
        except Exception:  # noqa: BLE001
            _LOG.exception(
                "entity resolver: alias upsert failed for %s", entity_id
            )
