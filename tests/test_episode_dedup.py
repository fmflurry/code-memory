"""Episode dedup by prompt content hash."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from code_memory.episodic.sqlite_store import Episode, EpisodicStore


def test_same_prompt_dedupes_to_single_row(tmp_path: Path) -> None:
    store = EpisodicStore(path=tmp_path / "ep.db")
    id1 = store.add(Episode(prompt="how good are we with order creation ?"))
    id2 = store.add(Episode(prompt="how good are we with order creation ?"))
    assert id1 == id2
    (n,) = store.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()
    assert n == 1


def test_dedup_refreshes_ts_to_latest(tmp_path: Path) -> None:
    store = EpisodicStore(path=tmp_path / "ep.db")
    ep_id = store.add(Episode(prompt="hello", ts=100.0))
    store.add(Episode(prompt="hello", ts=200.0))
    got = store.get(ep_id)
    assert got is not None
    assert got.ts == 200.0


def test_dedup_merges_tags_union_and_meta_dict(tmp_path: Path) -> None:
    store = EpisodicStore(path=tmp_path / "ep.db")
    ep_id = store.add(
        Episode(prompt="hi", tags=["a", "b"], meta={"k": 1, "x": "old"})
    )
    store.add(Episode(prompt="hi", tags=["b", "c"], meta={"x": "new", "y": 2}))
    got = store.get(ep_id)
    assert got is not None
    assert got.tags == ["a", "b", "c"]
    assert got.meta == {"k": 1, "x": "new", "y": 2}


def test_dedup_fills_null_fields_from_new_episode(tmp_path: Path) -> None:
    store = EpisodicStore(path=tmp_path / "ep.db")
    ep_id = store.add(Episode(prompt="hi"))
    store.add(Episode(prompt="hi", plan="P", patch="PATCH", verdict="pass"))
    got = store.get(ep_id)
    assert got is not None
    assert got.plan == "P"
    assert got.patch == "PATCH"
    assert got.verdict == "pass"


def test_dedup_preserves_first_non_null_fields(tmp_path: Path) -> None:
    store = EpisodicStore(path=tmp_path / "ep.db")
    ep_id = store.add(Episode(prompt="hi", verdict="pass"))
    store.add(Episode(prompt="hi", verdict="fail"))
    got = store.get(ep_id)
    assert got is not None
    # COALESCE keeps the first non-null; new value only fills NULL.
    assert got.verdict == "pass"


def test_distinct_prompts_stay_separate(tmp_path: Path) -> None:
    store = EpisodicStore(path=tmp_path / "ep.db")
    id1 = store.add(Episode(prompt="prompt one"))
    id2 = store.add(Episode(prompt="prompt two"))
    assert id1 != id2
    (n,) = store.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()
    assert n == 2


def test_whitespace_normalized_in_hash(tmp_path: Path) -> None:
    store = EpisodicStore(path=tmp_path / "ep.db")
    id1 = store.add(Episode(prompt="same"))
    id2 = store.add(Episode(prompt="  same\n"))
    assert id1 == id2


def test_dedupe_method_compacts_legacy_rows(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        """
        CREATE TABLE episodes (
            id TEXT PRIMARY KEY,
            ts REAL NOT NULL,
            prompt TEXT NOT NULL,
            plan TEXT, patch TEXT, verdict TEXT, tags TEXT, meta TEXT
        );
        """
    )
    legacy.executemany(
        "INSERT INTO episodes(id, ts, prompt) VALUES (?, ?, ?)",
        [
            ("a", 100.0, "dup"),
            ("b", 200.0, "dup"),
            ("c", 150.0, "dup"),
            ("d", 50.0, "unique"),
        ],
    )
    legacy.commit()
    legacy.close()

    store = EpisodicStore(path=db)
    removed = store.dedupe()

    # one group ("dup") with 3 rows -> keep 1, remove 2
    assert sum(len(v) for v in removed.values()) == 2
    assert len(removed) == 1
    (kept_id,) = list(removed.keys())
    # Oldest row wins (a, ts=100); ts refreshed to MAX (200).
    assert kept_id == "a"
    kept = store.get("a")
    assert kept is not None
    assert kept.ts == 200.0
    # Unique row untouched.
    assert store.get("d") is not None
    # Other dupes deleted.
    assert store.get("b") is None
    assert store.get("c") is None


def test_dedupe_no_op_when_already_unique(tmp_path: Path) -> None:
    store = EpisodicStore(path=tmp_path / "ep.db")
    store.add(Episode(prompt="one"))
    store.add(Episode(prompt="two"))
    removed = store.dedupe()
    assert removed == {}
