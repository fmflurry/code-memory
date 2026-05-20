"""Per-repo ingest state: track the last commit successfully ingested.

Lives in the same SQLite DB as episodes (per-project namespaced) so it
inherits project isolation automatically.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_state (
    repo_root TEXT PRIMARY KEY,
    last_sha  TEXT NOT NULL,
    last_ts   REAL NOT NULL,
    branch    TEXT
);
"""


@dataclass(frozen=True)
class IngestState:
    repo_root: str
    last_sha: str
    last_ts: float
    branch: str | None = None


class IngestStateStore:
    """Thin SQLite wrapper for per-repo ingest checkpoints."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def get(self, repo_root: str | Path) -> IngestState | None:
        row = self.conn.execute(
            "SELECT repo_root, last_sha, last_ts, branch FROM ingest_state WHERE repo_root = ?",
            (str(Path(repo_root).resolve()),),
        ).fetchone()
        if row is None:
            return None
        return IngestState(repo_root=row[0], last_sha=row[1], last_ts=row[2], branch=row[3])

    def set(self, repo_root: str | Path, sha: str, branch: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO ingest_state(repo_root, last_sha, last_ts, branch) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(repo_root) DO UPDATE SET "
            "  last_sha = excluded.last_sha, "
            "  last_ts  = excluded.last_ts, "
            "  branch   = excluded.branch",
            (str(Path(repo_root).resolve()), sha, time.time(), branch),
        )
        self.conn.commit()

    def clear(self, repo_root: str | Path) -> None:
        self.conn.execute(
            "DELETE FROM ingest_state WHERE repo_root = ?",
            (str(Path(repo_root).resolve()),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
