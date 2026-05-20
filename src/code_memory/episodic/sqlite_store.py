from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import CONFIG

SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(ts);
CREATE INDEX IF NOT EXISTS idx_episodes_verdict ON episodes(verdict);
"""


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


class EpisodicStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CONFIG.episodic_db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def add(self, ep: Episode) -> str:
        self.conn.execute(
            "INSERT INTO episodes(id, ts, prompt, plan, patch, verdict, tags, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ep.id,
                ep.ts,
                ep.prompt,
                ep.plan,
                ep.patch,
                ep.verdict,
                json.dumps(ep.tags),
                json.dumps(ep.meta),
            ),
        )
        self.conn.commit()
        return ep.id

    def get(self, ep_id: str) -> Episode | None:
        row = self.conn.execute(
            "SELECT id, ts, prompt, plan, patch, verdict, tags, meta "
            "FROM episodes WHERE id = ?",
            (ep_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_episode(row)

    def recent(self, limit: int = 20) -> list[Episode]:
        rows = self.conn.execute(
            "SELECT id, ts, prompt, plan, patch, verdict, tags, meta "
            "FROM episodes ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_episode(r) for r in rows]

    def by_ids(self, ids: list[str]) -> list[Episode]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT id, ts, prompt, plan, patch, verdict, tags, meta "
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
