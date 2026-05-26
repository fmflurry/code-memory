from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import CONFIG

# Base table — kept minimal so a legacy DB opens without errors. Every
# additional column lives in ``_MIGRATIONS`` so loading an old database
# transparently catches it up to the latest schema.
_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    prompt TEXT NOT NULL,
    plan TEXT,
    patch TEXT,
    verdict TEXT,
    tags TEXT,
    meta TEXT
);
"""

# Idempotent migrations. Each statement is run independently; failures
# (e.g. "duplicate column" when the migration has already been applied)
# are swallowed because that's the success path for an idempotent
# migration. Indexes that reference migration-added columns must come
# AFTER the corresponding ADD COLUMN, hence interleaved here.
_MIGRATIONS = (
    "ALTER TABLE episodes ADD COLUMN head_sha TEXT",
    "CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(ts)",
    "CREATE INDEX IF NOT EXISTS idx_episodes_verdict ON episodes(verdict)",
    "CREATE INDEX IF NOT EXISTS idx_episodes_head_sha ON episodes(head_sha)",
    # Content hash for dedup. Same user prompt re-asserted across turns
    # produced one row per assertion before; now the existing row gets
    # its ``ts`` refreshed and the new insert is a no-op. Non-unique by
    # design so legacy rows (NULL hash) still load without conflict.
    "ALTER TABLE episodes ADD COLUMN content_hash TEXT",
    "CREATE INDEX IF NOT EXISTS idx_episodes_content_hash ON episodes(content_hash)",
)


def _content_hash(prompt: str) -> str:
    """SHA-256 over the user prompt, normalized.

    Dedup key is prompt-only on purpose: the same prompt typed twice
    represents the same intent, regardless of which plan/patch/verdict
    the agent eventually produced. Whitespace is normalized so a
    trailing newline doesn't split otherwise-identical rows.
    """
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()


@dataclass
class Episode:
    prompt: str
    plan: str | None = None
    patch: str | None = None
    verdict: str | None = None  # pass | fail | partial
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: float = field(default_factory=time.time)
    # Git HEAD at the moment the episode was recorded — links the
    # agent's work back to the code state the graph was indexing then.
    head_sha: str | None = None


class EpisodicStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CONFIG.episodic_db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(_BASE_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                # column already added by a prior process — that's the
                # success path for an idempotent migration
                pass
        self.conn.commit()

    def add(self, ep: Episode) -> str:
        """Insert an episode, deduping on prompt content.

        If an existing row has the same ``content_hash``, refresh its
        ``ts`` to ``ep.ts`` and fill any previously-NULL fields from
        the new episode (plan/patch/verdict/head_sha). Tags are unioned
        and meta is merged with new values winning on key collision.
        Returns the existing row's id so vector upserts stay idempotent.
        """
        hash_ = _content_hash(ep.prompt)
        existing = self.conn.execute(
            "SELECT id, plan, patch, verdict, head_sha, tags, meta "
            "FROM episodes WHERE content_hash = ? LIMIT 1",
            (hash_,),
        ).fetchone()
        if existing is not None:
            return self._refresh_existing(existing, ep)

        self.conn.execute(
            "INSERT INTO episodes(id, ts, prompt, plan, patch, verdict, tags, meta, head_sha, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ep.id,
                ep.ts,
                ep.prompt,
                ep.plan,
                ep.patch,
                ep.verdict,
                json.dumps(ep.tags),
                json.dumps(ep.meta),
                ep.head_sha,
                hash_,
            ),
        )
        self.conn.commit()
        return ep.id

    def _refresh_existing(
        self, existing: tuple[Any, ...], ep: Episode
    ) -> str:
        existing_id = str(existing[0])
        old_plan, old_patch, old_verdict, old_head = (
            existing[1],
            existing[2],
            existing[3],
            existing[4],
        )
        old_tags = json.loads(existing[5]) if existing[5] else []
        old_meta = json.loads(existing[6]) if existing[6] else {}

        merged_tags = list(dict.fromkeys([*old_tags, *ep.tags]))
        merged_meta = {**old_meta, **ep.meta}

        self.conn.execute(
            "UPDATE episodes SET "
            "  ts = ?, "
            "  plan = COALESCE(plan, ?), "
            "  patch = COALESCE(patch, ?), "
            "  verdict = COALESCE(verdict, ?), "
            "  head_sha = COALESCE(head_sha, ?), "
            "  tags = ?, "
            "  meta = ? "
            "WHERE id = ?",
            (
                ep.ts,
                ep.plan if ep.plan else None,
                ep.patch if ep.patch else None,
                ep.verdict if ep.verdict else None,
                ep.head_sha,
                json.dumps(merged_tags),
                json.dumps(merged_meta),
                existing_id,
            ),
        )
        self.conn.commit()
        return existing_id

    def dedupe(self) -> dict[str, list[str]]:
        """Compact pre-existing duplicates in the table.

        For each ``content_hash`` group with >1 row, keep the row with
        the oldest ``ts`` (first observation), update its ``ts`` to
        ``MAX(ts)`` of the group so retrieval still surfaces it as
        recent, and delete the rest. Returns ``{kept_id: [removed_ids]}``
        so callers (e.g. the orchestrator) can prune matching vectors.

        Backfills ``content_hash`` for legacy NULL rows on the fly.
        """
        null_rows = self.conn.execute(
            "SELECT id, prompt FROM episodes WHERE content_hash IS NULL"
        ).fetchall()
        for ep_id, prompt in null_rows:
            self.conn.execute(
                "UPDATE episodes SET content_hash = ? WHERE id = ?",
                (_content_hash(prompt), ep_id),
            )
        if null_rows:
            self.conn.commit()

        groups = self.conn.execute(
            "SELECT content_hash FROM episodes "
            "WHERE content_hash IS NOT NULL "
            "GROUP BY content_hash HAVING COUNT(*) > 1"
        ).fetchall()

        removed: dict[str, list[str]] = {}
        for (hash_,) in groups:
            rows = self.conn.execute(
                "SELECT id, ts FROM episodes WHERE content_hash = ? "
                "ORDER BY ts ASC",
                (hash_,),
            ).fetchall()
            keep_id = str(rows[0][0])
            max_ts = max(float(r[1]) for r in rows)
            del_ids = [str(r[0]) for r in rows[1:]]
            self.conn.execute(
                "UPDATE episodes SET ts = ? WHERE id = ?", (max_ts, keep_id)
            )
            self.conn.executemany(
                "DELETE FROM episodes WHERE id = ?", [(d,) for d in del_ids]
            )
            removed[keep_id] = del_ids
        self.conn.commit()
        return removed

    def get(self, ep_id: str) -> Episode | None:
        row = self.conn.execute(
            "SELECT id, ts, prompt, plan, patch, verdict, tags, meta, head_sha "
            "FROM episodes WHERE id = ?",
            (ep_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_episode(row)

    def recent(self, limit: int = 20) -> list[Episode]:
        rows = self.conn.execute(
            "SELECT id, ts, prompt, plan, patch, verdict, tags, meta, head_sha "
            "FROM episodes ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_episode(r) for r in rows]

    def by_ids(self, ids: list[str]) -> list[Episode]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT id, ts, prompt, plan, patch, verdict, tags, meta, head_sha "
            f"FROM episodes WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        return [_row_to_episode(r) for r in rows]

    def close(self) -> None:
        self.conn.close()


def _row_to_episode(row: tuple[Any, ...]) -> Episode:
    return Episode(
        id=row[0],
        ts=row[1],
        prompt=row[2],
        plan=row[3],
        patch=row[4],
        verdict=row[5],
        tags=json.loads(row[6]) if row[6] else [],
        meta=json.loads(row[7]) if row[7] else {},
        head_sha=row[8] if len(row) > 8 else None,
    )


def episode_text(ep: Episode) -> str:
    """Composite text for embedding."""
    parts = [f"PROMPT:\n{ep.prompt}"]
    if ep.plan:
        parts.append(f"PLAN:\n{ep.plan}")
    if ep.patch:
        parts.append(f"PATCH:\n{ep.patch}")
    if ep.verdict:
        parts.append(f"VERDICT: {ep.verdict}")
    return "\n\n".join(parts)


def episode_payload(ep: Episode) -> dict[str, Any]:
    d = asdict(ep)
    d.pop("plan", None)
    d.pop("patch", None)
    return d
