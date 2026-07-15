"""RED tests pinning the CORRECT sqlite-connection lifecycle for the
``watchd`` daemon (architect-ruled Option A).

Background
----------
``EpisodicStore``/``IngestStateStore`` open sqlite with the default
``check_same_thread=True``. The daemon fires each watched root's sync
from a brand-new ``threading.Timer`` thread per debounce
(``sync/watcher.py``: ``Debouncer.bump`` -> ``threading.Timer(...,
self._fire)`` -> ``self.flush()`` -> ``Watcher._trigger_sync`` /
``MultiRootWatcher._trigger_sync``). Caching a sqlite connection across
calls (the rejected "option B") means the SECOND sync of a project runs
on a different OS thread than the one that opened the cached connection
and blows up with ``sqlite3.ProgrammingError: SQLite objects created in
a thread can only be used in that same thread`` — currently true of
``sync.py``'s standalone read at line 132
(``get_ingest_state_store(cfg.episodic_db)``). Separately, every
``Pipeline(...)`` constructed inside ``sync.py`` opens two more sqlite
connections (``EpisodicStore`` + ``IngestStateStore``) that are never
closed — a per-call leak in a long-lived daemon process.

The architect-ruled fix ("option A") is per-call, ``with``-scoped
resource ownership: every ``Pipeline(...)`` site in ``sync.py`` becomes
``with Pipeline(project=slug) as pipe: ...`` (closes its own two sqlite
stores on the calling thread every time — leak-free AND thread-safe,
since a new connection is opened and closed on whichever thread runs
that particular call), and the standalone prior-state read at
``sync.py:132`` becomes ``with IngestStateStore(cfg.episodic_db) as
state_store: prior = state_store.get(root_path)`` instead of the
long-lived process-cached accessor.

None of this exists yet. Every test below must fail for a specific,
diagnosable reason (cross-thread ``sqlite3.ProgrammingError``, missing
context-manager protocol, or a real leak) — not because of a bug in
this test file.
"""

from __future__ import annotations

import gc
import queue
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from code_memory.config import CONFIG

# A single leaked handle (e.g. the long-lived module cache itself, if it
# still exists after the fix landed defensively) is tolerated; unbounded
# growth proportional to the number of sync_repo calls is not.
_LEAK_TOLERANCE = 5


class _PersistentWorker:
    """An always-alive background thread that runs submitted callables one
    at a time, in submission order, and blocks the caller until each is
    done.

    Why this exists instead of ``t = threading.Thread(...); t.start();
    t.join()`` per call (which is the literal shape of a
    ``threading.Timer``-per-debounce-fire in ``sync/watcher.py``):
    measured on this suite's CI/dev macOS + CPython combination, a brand
    new ``threading.Thread`` created immediately after the previous one
    was joined is reliably handed back the *exact same* native thread
    identity by the platform's thread allocator (verified empirically —
    10/10 and then 150/150 sequential start-then-join cycles all reused
    one identical OS thread id). Since sqlite3's ``check_same_thread``
    guard (and any correctness check built on ``threading.get_ident()``)
    keys off that same native identity, a strictly-sequential
    create-join-create-join loop can silently fail to ever exercise a
    genuine cross-thread call — which would make this test flaky (mostly
    green, rarely red) rather than a reliable pin of the bug.

    Two independent, persistent ``_PersistentWorker``s give two stable,
    genuinely distinct OS threads for the lifetime of the test — the
    property that actually matters here (a project's Nth sync running on
    a *different* thread than its 1st), without depending on how eagerly
    any particular platform recycles freed thread stacks. This is a
    closer functional match to production anyway: a long-lived daemon
    process has many other threads coming and going, making the specific
    stack-slot-reuse collision this test's naive form suffered from far
    less likely there than in an otherwise-idle test process.
    """

    def __init__(self) -> None:
        self._tasks: queue.Queue[tuple[Callable[[], None], threading.Event] | None] = (
            queue.Queue()
        )
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            item = self._tasks.get()
            if item is None:
                return
            fn, done = item
            try:
                fn()
            finally:
                done.set()

    def run(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on this worker's persistent thread; block until done."""
        done = threading.Event()
        self._tasks.put((fn, done))
        done.wait()

    def stop(self) -> None:
        self._tasks.put(None)
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path, *, files: dict[str, str] | None = None) -> str:
    """Create a small tracked git repo; return HEAD sha."""
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t.test"],
        ["config", "user.name", "Test"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    files = files or {"a.py": "a = 1\n", "b.py": "b = 2\n", "c.py": "c = 3\n"}
    for name, content in files.items():
        (repo / name).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True, capture_output=True
    )
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


class _FakeVector:
    """Stands in for QdrantStore — no real network, full method surface
    that the production ingest path (skip_vectors=False, the default
    ``sync.py`` uses) actually calls."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    def ensure_collection(self, name: str) -> None:
        pass

    def delete_by_path(self, collection: str, path: str) -> None:
        pass

    def delete_by_ids(self, collection: str, ids: list[str]) -> None:
        pass

    def upsert(self, collection: str, records: list[Any]) -> None:
        pass


class _FakeGraph:
    """Stands in for FalkorStore — no real network."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    def ensure_indexes(self) -> None:
        pass

    def delete_file(self, path: str, *, head_sha: str | None = None, head_ord: int | None = None) -> None:
        pass

    def upsert_nodes(self, nodes: list[Any], *, head_sha: str | None = None, head_ord: int | None = None) -> None:
        pass

    def upsert_edges(self, edges: list[Any], *, head_sha: str | None = None, head_ord: int | None = None) -> None:
        pass

    def count_symbols(self) -> int:
        return 0


class _FakeEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_one(self, text: str) -> list[float]:
        return [0.0, 0.0, 0.0, 0.0]


def _patch_pipeline_network_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch only the network-backed collaborators (Qdrant, Falkor, the
    embedder). ``EpisodicStore``/``IngestStateStore`` are deliberately
    left REAL — the whole point of these tests is to exercise the actual
    sqlite connection lifecycle across real OS threads.
    """
    from code_memory.orchestrator import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "QdrantStore", _FakeVector)
    monkeypatch.setattr(pipeline_mod, "FalkorStore", _FakeGraph)
    monkeypatch.setattr(pipeline_mod, "get_embedder", lambda: _FakeEmbedder())


def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point every module's ``CONFIG`` at an isolated tmp data_dir so
    ``episodic_db`` never touches the real ``./data`` tree."""
    from code_memory.orchestrator import pipeline as pipeline_mod
    from code_memory.sync import sync as sync_mod

    isolated = replace(CONFIG, data_dir=tmp_path / "data")
    monkeypatch.setattr(sync_mod, "CONFIG", isolated)
    monkeypatch.setattr(pipeline_mod, "CONFIG", isolated)
    return isolated


# ---------------------------------------------------------------------------
# 1. Headline: production-path threaded leak + correctness test.
#
#    A Pipeline-in-isolation test is NOT sufficient here — the bug only
#    reproduces when sync_repo runs on a genuinely different OS thread
#    per call, mirroring watcher.py's Debouncer firing each debounced
#    sync from a fresh threading.Timer thread.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(600)
# Not hung, just slow on Windows: every sync_repo spawns 8-10 git
# subprocesses, and process creation there (plus EDR scanning) costs
# ~100ms each — 150 iterations legitimately take ~3 minutes, past the
# suite-wide 120s ceiling.
def test_sync_repo_across_many_threaded_calls_is_thread_safe_and_leak_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the daemon's real firing pattern: N debounced syncs, each
    on its own thread (a new ``threading.Timer`` thread per fire), against
    one dirty tracked file per iteration so every call takes the
    dirty_only path (never noop) and actually exercises Pipeline.

    Must hold, post-fix:
      A) no ``IngestStateStore`` instance is ever ``.get()``-ed from a
         thread other than the one that constructed it (today: the
         cached instance from ``get_ingest_state_store`` at sync.py:132
         is built once on the first call's thread and reused — silently
         or as a real ``sqlite3.ProgrammingError`` depending on whether
         the OS happens to recycle that thread id, which is why we
         instrument the actual thread-affinity contract directly below
         rather than only hoping a crash reproduces).
      B) no thread raises, and no result is silently swallowed.
      C) every call's SyncResult.action == "dirty_only".
      D) sqlite3.Connection objects don't accumulate roughly 2x per call
         (today: Pipeline() is never closed, so EpisodicStore + a second
         IngestStateStore leak on every call that gets far enough to
         construct one).
    """
    from code_memory.orchestrator import ingest_state as ingest_state_mod
    from code_memory.sync import sync as sync_mod

    monkeypatch.setattr(ingest_state_mod, "_INGEST_STATE_STORE_CACHE", {}, raising=False)
    _patch_pipeline_network_backends(monkeypatch)
    isolated_cfg = _isolate_config(monkeypatch, tmp_path)

    # ---- Deterministic thread-affinity instrumentation -------------------
    # sqlite3's own check_same_thread guard compares OS-level thread ids,
    # which macOS/CPython can (and does) recycle across short-lived,
    # strictly-sequential (start -> join -> start) threads — so waiting
    # for a real sqlite3.ProgrammingError to fire is a coin flip, not a
    # reliable RED signal. Instead we record, in pure Python, which thread
    # *constructed* every IngestStateStore and which thread(s) later call
    # ``.get()`` on it. Any mismatch is the exact contract violation that
    # makes option B unsafe, independent of whether sqlite3's own guard
    # happens to notice on this particular OS/run.
    creator_thread_by_store: dict[int, int] = {}
    affinity_violations: list[str] = []
    real_init = ingest_state_mod.IngestStateStore.__init__
    real_get = ingest_state_mod.IngestStateStore.get

    def _tracking_init(self: Any, *a: Any, **kw: Any) -> None:
        real_init(self, *a, **kw)
        creator_thread_by_store[id(self)] = threading.get_ident()

    def _tracking_get(self: Any, *a: Any, **kw: Any) -> Any:
        creator = creator_thread_by_store.get(id(self))
        current = threading.get_ident()
        if creator is not None and creator != current:
            affinity_violations.append(
                f"IngestStateStore(id={id(self)}) constructed on thread "
                f"{creator} but .get() called from thread {current}"
            )
        return real_get(self, *a, **kw)

    monkeypatch.setattr(ingest_state_mod.IngestStateStore, "__init__", _tracking_init)
    monkeypatch.setattr(ingest_state_mod.IngestStateStore, "get", _tracking_get)

    repo = tmp_path / "repo"
    tracked = {"a.py": "a = 1\n", "b.py": "b = 2\n", "c.py": "c = 3\n"}
    head = _init_git_repo(repo, files=tracked)
    slug = "watchd-threaded-lifecycle-test"
    cfg = isolated_cfg.for_project(slug)

    # Pre-seed ingest state so HEAD already matches: every iteration then
    # dirties one tracked file (not committed) so the decision tree takes
    # Case 1 -> dirty_only, never noop and never full_ingest/incremental.
    seed = ingest_state_mod.IngestStateStore(cfg.episodic_db)
    seed.set(repo, sha=head, branch="main")
    seed.close()

    N = 150
    files = sorted(tracked.keys())

    gc.collect()
    conns_before = sum(isinstance(o, sqlite3.Connection) for o in gc.get_objects())

    # Two stable, genuinely distinct persistent threads, alternated by
    # iteration parity — see _PersistentWorker's docstring for why this
    # is used instead of a fresh threading.Thread per call. The property
    # under test — a project's sync running on a different real OS
    # thread across calls — is exercised on every odd/even boundary.
    workers = [_PersistentWorker(), _PersistentWorker()]
    outcomes: list[dict[str, Any]] = []
    try:
        for i in range(N):
            target = repo / files[i % len(files)]
            target.write_text(f"value = {i}\n")

            outcome: dict[str, Any] = {}

            def _run(_outcome: dict[str, Any] = outcome) -> None:
                try:
                    _outcome["result"] = sync_mod.sync_repo(
                        repo, project=slug, trigger="test", fetch=False
                    )
                except Exception as exc:  # noqa: BLE001 - captured for the main thread
                    _outcome["exc"] = exc

            workers[i % 2].run(_run)
            outcomes.append(outcome)
    finally:
        for w in workers:
            w.stop()

    gc.collect()
    conns_after = sum(isinstance(o, sqlite3.Connection) for o in gc.get_objects())

    # ---- Assertion A (primary, deterministic): no cross-thread reuse ----
    if affinity_violations:
        raise AssertionError(
            f"{len(affinity_violations)} cross-thread IngestStateStore "
            f"reuse(s) detected across {N} threaded sync_repo calls "
            f"(first: {affinity_violations[0]}). sync.py must read prior "
            "ingest state via a short-lived `with IngestStateStore(...)` "
            "block opened fresh on the calling thread every time, not "
            "the long-lived get_ingest_state_store() process cache."
        )

    # ---- Assertion B: thread-safety — no swallowed/raised exceptions ----
    failures = [(i, o["exc"]) for i, o in enumerate(outcomes) if "exc" in o]
    if failures:
        first_idx, first_exc = failures[0]
        raise AssertionError(
            f"{len(failures)}/{N} threaded sync_repo calls raised an "
            f"exception (first at iteration {first_idx}: {first_exc!r}). "
            "This is the cross-thread sqlite3.ProgrammingError from "
            "reusing a cached IngestStateStore connection across "
            "threading.Timer-fired threads (sync.py:132)."
        ) from first_exc

    # ---- Assertion C: every call actually did dirty_only work, not a
    # swallowed/misrouted action -------------------------------------------
    for i, o in enumerate(outcomes):
        assert o["result"].action == "dirty_only", (
            f"iteration {i}: expected action='dirty_only', got "
            f"{o['result'].action!r} (notes={o['result'].notes!r})"
        )

    # ---- Assertion D: no ~2x-per-call sqlite connection leak --------------
    grown = conns_after - conns_before
    assert grown <= _LEAK_TOLERANCE, (
        f"leaked ~{grown} sqlite3.Connection objects across {N} sync_repo "
        f"calls (before={conns_before}, after={conns_after}); expected "
        f"<= {_LEAK_TOLERANCE}. Every Pipeline(...) built inside sync.py "
        "must be closed on every call (`with Pipeline(project=slug) as "
        "pipe:`), not just constructed and dropped."
    )


# ---------------------------------------------------------------------------
# 2. IngestStateStore / EpisodicStore are context managers.
# ---------------------------------------------------------------------------


def test_ingest_state_store_is_a_context_manager_and_closes_on_exit(
    tmp_path: Path,
) -> None:
    from code_memory.orchestrator.ingest_state import IngestStateStore

    db_path = tmp_path / "state.sqlite"
    repo = tmp_path / "repo"
    repo.mkdir()

    with IngestStateStore(db_path) as s:
        assert s is not None
        s.set(repo, sha="deadbeef", branch="main")
        assert s.get(repo) is not None

    # __exit__ must have closed the underlying sqlite connection: any
    # further use raises sqlite3.ProgrammingError against a closed db.
    with pytest.raises(sqlite3.ProgrammingError):
        s.get(repo)

    # close() must stay idempotent after __exit__ already closed it.
    s.close()
    s.close()


def test_episodic_store_is_a_context_manager_and_closes_on_exit(
    tmp_path: Path,
) -> None:
    from code_memory.episodic.sqlite_store import Episode, EpisodicStore

    db_path = tmp_path / "episodes.sqlite"

    with EpisodicStore(path=db_path) as s:
        assert s is not None
        ep_id = s.add(Episode(prompt="hello world"))
        assert s.get(ep_id) is not None

    with pytest.raises(sqlite3.ProgrammingError):
        s.recent()

    s.close()
    s.close()


# ---------------------------------------------------------------------------
# 3. sync.py's clean-HEAD/noop read must use a short-lived, closed
#    connection — not the long-lived process-cached accessor.
# ---------------------------------------------------------------------------


def test_noop_sync_reads_prior_state_via_short_lived_connection_not_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins that ``sync.py``'s standalone prior-state read opens
    ``IngestStateStore(cfg.episodic_db)`` in a ``with`` block (closed
    right after the read) instead of ``get_ingest_state_store(...)``,
    whose whole purpose is to hand back the SAME long-lived connection
    on every call — which is exactly what crashes cross-thread.

    Two checks:
      1. After a noop sync_repo call, the module-level cache used by
         ``get_ingest_state_store`` holds no entry for this db path —
         i.e. sync.py never even touched that accessor.
      2. A second sync_repo call for the same repo, issued from a
         DIFFERENT thread, still succeeds cleanly (no
         sqlite3.ProgrammingError) — the fix must work not just once but
         repeatedly, from arbitrary threads.
    """
    from code_memory.orchestrator import ingest_state as ingest_state_mod
    from code_memory.sync import sync as sync_mod

    monkeypatch.setattr(ingest_state_mod, "_INGEST_STATE_STORE_CACHE", {}, raising=False)
    isolated_cfg = _isolate_config(monkeypatch, tmp_path)

    repo = tmp_path / "repo"
    head = _init_git_repo(repo)
    slug = "watchd-noop-cache-test"
    cfg = isolated_cfg.for_project(slug)

    seed = ingest_state_mod.IngestStateStore(cfg.episodic_db)
    seed.set(repo, sha=head, branch="main")
    seed.close()

    result_a = sync_mod.sync_repo(repo, project=slug, trigger="test", fetch=False)
    assert result_a.action == "noop"

    cache_key = str(Path(cfg.episodic_db).resolve())
    assert cache_key not in ingest_state_mod._INGEST_STATE_STORE_CACHE, (
        "sync.py's noop-path prior-state read must not populate (or use) "
        "the long-lived get_ingest_state_store() process cache — it must "
        "open IngestStateStore(cfg.episodic_db) in a `with` block that "
        "closes immediately after the read."
    )

    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["result"] = sync_mod.sync_repo(
                repo, project=slug, trigger="test", fetch=False
            )
        except Exception as exc:  # noqa: BLE001
            outcome["exc"] = exc

    t = threading.Thread(target=_run, name="second-noop-thread")
    t.start()
    t.join()

    assert "exc" not in outcome, (
        f"second (cross-thread) noop sync_repo call raised: "
        f"{outcome.get('exc')!r} — this is the cross-thread "
        "sqlite3.ProgrammingError from a cached connection"
    )
    assert outcome["result"].action == "noop"
