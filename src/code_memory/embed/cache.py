"""Persistent content-hash embedding cache.

Most enterprise workflows re-ingest the same repo daily after small
diffs: a few changed files, the rest stable. Without a cache, every
ingest re-embeds 100% of the corpus — for a 134k-chunk monorepo on
``bge-m3``/Ollama that's ~1.5 hours of pure inference each run.

This cache fingerprints each chunk's text (SHA-256) plus the embedding
model name and keys a dense / sparse vector pair on the result. On
re-ingest, unchanged chunks short-circuit the embedder entirely. Only
new or modified chunks pay the model cost.

Design choices:

- **SQLite single-file store** so it shares the same lifecycle as
  ``EpisodicStore`` (one persistent state directory per project). No
  separate daemon.
- **Per-model namespacing.** Switching between ``bge-m3`` and
  ``bge-small-en`` must not pollute results — they live in different
  rows. Same hash + different model = different cache entries.
- **Raw float32 BLOBs.** Lighter than JSON; deserialises with a single
  ``struct.unpack`` call.
- **Insert-only by default.** Cache is treated as monotonic; a separate
  ``vacuum`` clears stale entries that haven't been read in N days.
- **No locking beyond SQLite's default.** Concurrent watch + manual
  ingest are rare and the upsert path uses ``INSERT OR REPLACE`` so
  the latest write wins without explicit serialisation.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import struct
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from .m3 import HybridVec, SparseVec

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embed_cache (
    chunk_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    dense BLOB NOT NULL,
    sparse_idx BLOB,
    sparse_val BLOB,
    ts REAL NOT NULL,
    PRIMARY KEY (chunk_hash, model)
);
CREATE INDEX IF NOT EXISTS idx_embed_cache_ts ON embed_cache(ts);
"""


def hash_chunk(text: str) -> str:
    """SHA-256 of UTF-8 chunk text. Stable, collision-resistant."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pack_floats(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def _unpack_floats(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _pack_ints(values: Sequence[int]) -> bytes:
    return struct.pack(f"<{len(values)}i", *values)


def _unpack_ints(blob: bytes) -> list[int]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}i", blob))


class EmbedCache:
    """SQLite-backed content-hash cache for embedding vectors.

    Open once per process. Concurrent access is safe but uncoordinated
    — last write wins. The hot path (``get_many``) issues one
    parameterised ``SELECT … WHERE chunk_hash IN (…)`` and rebuilds the
    in-memory mapping; the cold path (``put_many``) batches inserts in
    one transaction.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so the pipeline + watcher can share
        # the same instance from different threads. SQLite serialises
        # writes internally; reads are concurrent.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        # Stats so callers can log hit/miss ratios.
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------ read

    def get_many(
        self, hashes: Iterable[str], model: str
    ) -> dict[str, HybridVec]:
        """Return ``{hash: HybridVec}`` for every cached hash in ``hashes``.

        Missing entries are simply absent from the result dict — the
        caller decides what to do (typically: build a miss-list and
        send it to the embedder).
        """
        hash_list = list(hashes)
        if not hash_list:
            return {}
        # SQLite's parameter limit is 999 by default; chunk to stay safe.
        out: dict[str, HybridVec] = {}
        for i in range(0, len(hash_list), 800):
            batch = hash_list[i : i + 800]
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"""
                SELECT chunk_hash, dense, sparse_idx, sparse_val
                FROM embed_cache
                WHERE model = ? AND chunk_hash IN ({placeholders})
                """,
                (model, *batch),
            ).fetchall()
            for chunk_hash, dense_blob, idx_blob, val_blob in rows:
                indices = _unpack_ints(idx_blob) if idx_blob else []
                values = _unpack_floats(val_blob) if val_blob else []
                out[chunk_hash] = HybridVec(
                    dense=_unpack_floats(dense_blob),
                    sparse=SparseVec(indices=indices, values=values),
                )
        self.hits += len(out)
        self.misses += len(hash_list) - len(out)
        return out

    # ------------------------------------------------------------ write

    def put_many(
        self,
        items: Iterable[tuple[str, HybridVec]],
        model: str,
    ) -> int:
        """Insert (hash, vec) pairs for ``model``. Returns count written."""
        rows = []
        now = time.time()
        for chunk_hash, vec in items:
            rows.append(
                (
                    chunk_hash,
                    model,
                    _pack_floats(vec.dense),
                    _pack_ints(vec.sparse.indices) if vec.sparse.indices else None,
                    _pack_floats(vec.sparse.values) if vec.sparse.values else None,
                    now,
                )
            )
        if not rows:
            return 0
        with self.conn:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO embed_cache
                    (chunk_hash, model, dense, sparse_idx, sparse_val, ts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    # ----------------------------------------------------------- admin

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM embed_cache"
        ).fetchone()
        return {
            "total_entries": int(rows[0]),
            "hits": self.hits,
            "misses": self.misses,
        }

    def vacuum_older_than(self, seconds: float) -> int:
        """Drop entries last touched before ``now - seconds``."""
        cutoff = time.time() - seconds
        cur = self.conn.execute(
            "DELETE FROM embed_cache WHERE ts < ?", (cutoff,)
        )
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> EmbedCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
