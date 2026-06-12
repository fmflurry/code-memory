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

_MIGRATIONS = [
    "ALTER TABLE ingest_state ADD COLUMN file_count INTEGER",
    "ALTER TABLE ingest_state ADD COLUMN symbol_count INTEGER",
]


@dataclass(frozen=True)
class IngestState:
    repo_root: str
    last_sha: str
    last_ts: float
    branch: str | None = None
    file_count: int | None = None
    symbol_count: int | None = None


class IngestStateStore:
    """Thin SQLite wrapper for per-repo ingest checkpoints."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

    def get(self, repo_root: str | Path) -> IngestState | None:
        # Attempt to read with the extended columns; fall back to the
        # legacy 4-column schema if the columns don't exist yet.
        try:
            row = self.conn.execute(
                "SELECT repo_root, last_sha, last_ts, branch, "
                "       file_count, symbol_count "
                "FROM ingest_state WHERE repo_root = ?",
                (str(Path(repo_root).resolve()),),
            ).fetchone()
        except sqlite3.OperationalError:
            row = self.conn.execute(
                "SELECT repo_root, last_sha, last_ts, branch "
                "FROM ingest_state WHERE repo_root = ?",
                (str(Path(repo_root).resolve()),),
            ).fetchone()
            if row is None:
                return None
            return IngestState(
                repo_root=row[0], last_sha=row[1], last_ts=row[2], branch=row[3]
            )
        if row is None:
            return None
        return IngestState(
            repo_root=row[0],
            last_sha=row[1],
            last_ts=row[2],
            branch=row[3],
            file_count=row[4],
            symbol_count=row[5],
        )

    def set(
        self,
        repo_root: str | Path,
        sha: str,
        branch: str | None = None,
        file_count: int | None = None,
        symbol_count: int | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO ingest_state(repo_root, last_sha, last_ts, branch, "
            "                        file_count, symbol_count) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(repo_root) DO UPDATE SET "
            "  last_sha = excluded.last_sha, "
            "  last_ts  = excluded.last_ts, "
            "  branch   = excluded.branch, "
            "  file_count   = excluded.file_count, "
            "  symbol_count = excluded.symbol_count",
            (str(Path(repo_root).resolve()), sha, time.time(), branch,
             file_count, symbol_count),
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
