"""Persist + load ``Episode.head_sha`` via SQLite."""

from __future__ import annotations

from pathlib import Path

from code_memory.episodic.sqlite_store import Episode, EpisodicStore


def test_episode_round_trips_head_sha(tmp_path: Path) -> None:
    store = EpisodicStore(path=tmp_path / "ep.db")
    ep_id = store.add(Episode(prompt="hi", head_sha="abc123"))
    got = store.get(ep_id)
    assert got is not None
    assert got.head_sha == "abc123"


def test_episode_head_sha_optional(tmp_path: Path) -> None:
    store = EpisodicStore(path=tmp_path / "ep.db")
    ep_id = store.add(Episode(prompt="hi"))
    got = store.get(ep_id)
    assert got is not None
    assert got.head_sha is None


def test_migration_adds_head_sha_column_to_legacy_db(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        """
        CREATE TABLE episodes (
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
    )
    legacy.execute(
        "INSERT INTO episodes(id, ts, prompt) VALUES ('legacy', 1.0, 'old prompt')"
    )
    legacy.commit()
    legacy.close()

    # Opening with the new store must migrate the schema in place.
    store = EpisodicStore(path=db)
    cols = {
        r[1] for r in store.conn.execute("PRAGMA table_info(episodes)").fetchall()
    }
    assert "head_sha" in cols
    legacy_ep = store.get("legacy")
    assert legacy_ep is not None
    assert legacy_ep.head_sha is None
