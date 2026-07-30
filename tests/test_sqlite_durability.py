"""Pins FIX B: WAL journal mode on both SQLite-backed stores
(``EmbedCache`` and ``MetricsStore``).

Verified root cause: both ``data/embed_cache.sqlite`` and
``data/metrics.db`` are ``PRAGMA journal_mode=delete``. Confirmed live
by the tests below (RED before the fix: ``assert 'delete' == 'wal'``).

IMPORTANT CORRECTION to the brief's framing on ``busy_timeout``: the
brief describes writes failing with ``OperationalError: database is
locked`` because "neither store sets busy_timeout". Measured directly
in this environment: **that part is not true**. CPython's
``sqlite3.connect(database, timeout=5.0)`` defaults ``timeout`` to
``5.0`` seconds and applies it via ``sqlite3_busy_timeout()`` under the
hood -- so every connection opened by ``EmbedCache`` and
``MetricsStore`` already has an effective 5000 ms busy timeout, with
no code change. Verified: ``sqlite3.connect(':memory:').execute(
'PRAGMA busy_timeout').fetchone() == (5000,)``, and a 30-thread /
1500-write stress test against a real DELETE-journal-mode file
produced zero ``OperationalError``s and zero lost rows. ``grep -rn
busy_timeout src/`` returning nothing is real, but it doesn't mean
writes are undefended -- the stdlib default already covers it.

So the busy_timeout tests below are **pinning / regression** tests
(already green today), not RED bugs -- they guard against a future
change (e.g. someone passing ``timeout=0`` while "cleaning up"
connection code) silently reintroducing the lock-loss failure mode.
The one genuine, confirmed defect in FIX B is the journal mode itself:
DELETE mode holds an exclusive lock that blocks *readers* too for the
duration of a writer's transaction, where WAL lets readers proceed
concurrently with a writer -- a real (if less dramatic than "database
is locked") contention cost that WAL removes.

Design decision pinned here: graceful degradation over hard failure
when WAL itself can't be enabled (e.g. some network mounts refuse WAL
because it needs shared-memory mmap support the mount doesn't
provide). Construction must never raise just because WAL didn't stick
-- fall through to whatever journal mode the filesystem does support.
An explicit ``PRAGMA busy_timeout=5000`` is still worth setting
unconditionally in the implementation (documents intent, doesn't rely
on an implicit stdlib default nobody asked for) even though it isn't
fixing an observed defect.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from code_memory.embed.cache import EmbedCache
from code_memory.embed.m3 import HybridVec, SparseVec
from code_memory.metrics import MetricsStore


def _vec(seed: float) -> HybridVec:
    return HybridVec(dense=[seed], sparse=SparseVec(indices=[], values=[]))


# ---------------------------------------------------------------------------
# EmbedCache
# ---------------------------------------------------------------------------


def test_embed_cache_opens_in_wal_mode(tmp_path: Path) -> None:
    cache = EmbedCache(tmp_path / "c.db")
    mode = cache.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    cache.close()


def test_embed_cache_sets_a_nonzero_busy_timeout(tmp_path: Path) -> None:
    """Pinning test -- already green today via CPython's stdlib default
    (``sqlite3.connect(..., timeout=5.0)``), see module docstring. Guards
    against a future change accidentally passing ``timeout=0``."""
    cache = EmbedCache(tmp_path / "c.db")
    timeout_ms = cache.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms > 0
    cache.close()


def test_embed_cache_wal_mode_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with EmbedCache(db) as cache:
        cache.put_many([("h1", _vec(1.0))], model="m")
    with EmbedCache(db) as cache2:
        mode = cache2.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        assert "h1" in cache2.get_many(["h1"], "m")


class _WalRejectingConnection:
    """Wraps a real sqlite3.Connection, rejecting only the WAL pragma.

    ``sqlite3.Connection`` is a builtin C-extension type in CPython and
    cannot have its ``execute`` method monkeypatched directly (it's
    immutable), so we intercept at the ``sqlite3.connect`` factory
    level instead and proxy everything else straight through to a real
    connection -- including the context-manager protocol used by
    ``EmbedCache.put_many``'s ``with self.conn:`` block.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if isinstance(sql, str) and "journal_mode" in sql.lower() and "wal" in sql.lower():
            raise sqlite3.OperationalError("disk I/O error: VFS does not support WAL")
        return self._real.execute(sql, *args, **kwargs)

    def __enter__(self) -> "_WalRejectingConnection":
        self._real.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.__exit__(*exc)

    def __getattr__(self, name: str):  # noqa: ANN001
        return getattr(self._real, name)


def test_embed_cache_degrades_gracefully_when_wal_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a filesystem (e.g. a network mount) that rejects WAL.
    Construction must not raise, and the cache must remain fully
    functional under whichever journal mode the filesystem does
    support -- this is the "degrade gracefully" branch of the design
    decision, not a hard fail."""
    real_connect = sqlite3.connect

    def _fake_connect(*args: object, **kwargs: object) -> _WalRejectingConnection:
        return _WalRejectingConnection(real_connect(*args, **kwargs))  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", _fake_connect)

    cache = EmbedCache(tmp_path / "c.db")  # must not raise
    cache.put_many([("h1", _vec(1.0))], model="m")
    assert "h1" in cache.get_many(["h1"], "m")
    cache.close()


def test_embed_cache_concurrent_writers_do_not_lose_entries(tmp_path: Path) -> None:
    """Real threads, separate connections (separate EmbedCache instances)
    against the same file -- the scenario from the bug report: MCP
    server + per-save reingest hook + watcher all writing concurrently.
    Pinning test -- already green today thanks to the stdlib default
    busy_timeout (see module docstring); kept as regression coverage
    for the actual concurrent-writer scenario the brief was worried
    about, independent of which mechanism prevents the lock error."""
    db = tmp_path / "c.db"
    EmbedCache(db).close()  # create schema once, up front

    n_threads = 8
    writes_per_thread = 15
    errors: list[BaseException] = []

    def _writer(idx: int) -> None:
        try:
            cache = EmbedCache(db)
            items = [
                (f"h-{idx}-{i}", _vec(float(i))) for i in range(writes_per_thread)
            ]
            cache.put_many(items, model="m")
            cache.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"writer thread(s) raised: {errors!r}"

    verify = EmbedCache(db)
    total = verify.conn.execute("SELECT COUNT(*) FROM embed_cache").fetchone()[0]
    verify.close()
    assert total == n_threads * writes_per_thread


# ---------------------------------------------------------------------------
# MetricsStore
# ---------------------------------------------------------------------------


def test_metrics_store_opens_in_wal_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "m.db"
    MetricsStore(db_path)  # triggers _init_db

    raw = sqlite3.connect(db_path)
    mode = raw.execute("PRAGMA journal_mode").fetchone()[0]
    raw.close()
    assert mode.lower() == "wal"


def test_metrics_store_concurrent_writers_do_not_lose_rows(tmp_path: Path) -> None:
    """Every MetricsStore method opens its own sqlite3.connect() per call
    (metrics.py). Pinning test -- already green today because each of
    those fresh connections still gets CPython's stdlib default
    busy_timeout (5000 ms); kept as regression coverage in case a
    future refactor introduces an explicit ``timeout=`` override that
    drops it."""
    db_path = tmp_path / "m.db"
    MetricsStore(db_path).close()

    n_threads = 8
    writes_per_thread = 15
    errors: list[BaseException] = []

    def _writer(idx: int) -> None:
        try:
            store = MetricsStore(db_path)
            for i in range(writes_per_thread):
                store.record_tool_call(
                    tool="t", project="p", query_text=f"{idx}-{i}"
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"writer thread(s) raised: {errors!r}"

    raw = sqlite3.connect(db_path)
    total = raw.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    raw.close()
    assert total == n_threads * writes_per_thread
