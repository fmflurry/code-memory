"""RED tests for the `watchd` long-lived-daemon memory-safety fixes.

An audit found pre-existing per-call resource leaks in code that assumed a
short-lived CLI process: a fresh ``QdrantClient``/``FalkorDB`` connection per
``QdrantStore()``/``FalkorStore()``, a fresh sqlite connection per
``Pipeline()``/``sync_repo()`` call, an unprotected ``ThreadPoolExecutor``
teardown in the full-ingest hot path, a racy ``get_embedder()`` cold start,
and no re-entrancy guard around ``sync_repo`` for the same repo. None of
these fixes exist yet — every test below must fail until the GREEN
implementation lands (missing accessor / method / integration point, not a
bug in this test file).

Every test is isolated with fakes/spies; none touch a real Qdrant,
FalkorDB, or embedding backend.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# 1. Singleton network clients (Qdrant + Falkor)
# ---------------------------------------------------------------------------


def test_get_qdrant_client_is_a_process_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_qdrant_client(url)` must return the SAME client object across
    calls for the same url — a daemon building N QdrantStore()s must not
    open N QdrantClient connections."""
    from code_memory.vector import qdrant_store as qs_mod

    monkeypatch.setattr(qs_mod, "_CLIENTS", {}, raising=False)

    build_count = {"n": 0}

    class _FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            build_count["n"] += 1

    monkeypatch.setattr(qs_mod, "QdrantClient", _FakeClient)

    get_qdrant_client = qs_mod.get_qdrant_client  # AttributeError until implemented
    c1 = get_qdrant_client("http://localhost:6333")
    c2 = get_qdrant_client("http://localhost:6333")
    assert c1 is c2
    assert build_count["n"] == 1


def test_two_qdrant_stores_share_one_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two independently-constructed QdrantStore() instances in one process
    must reuse the same underlying client, not open a second connection."""
    from code_memory.vector import qdrant_store as qs_mod

    monkeypatch.setattr(qs_mod, "_CLIENTS", {}, raising=False)

    build_count = {"n": 0}

    class _FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            build_count["n"] += 1

        def get_collections(self) -> Any:
            return MagicMock(collections=[])

    monkeypatch.setattr(qs_mod, "QdrantClient", _FakeClient)

    store1 = qs_mod.QdrantStore(url="http://localhost:6333")
    store2 = qs_mod.QdrantStore(url="http://localhost:6333")
    assert store1.client is store2.client
    assert build_count["n"] == 1


def test_get_falkor_db_is_a_process_singleton_keyed_by_host_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_falkor_db(host, port)` singleton, keyed by (host, port) so
    distinct Falkor endpoints still get distinct connections."""
    from code_memory.graph import falkor_store as fs_mod

    monkeypatch.setattr(fs_mod, "_DBS", {}, raising=False)

    build_count = {"n": 0}

    class _FakeDb:
        def __init__(self, *a: Any, **kw: Any) -> None:
            build_count["n"] += 1
            self.connection = MagicMock()

        def select_graph(self, name: str) -> Any:
            return MagicMock()

    monkeypatch.setattr(fs_mod, "FalkorDB", _FakeDb)

    get_falkor_db = fs_mod.get_falkor_db  # AttributeError until implemented
    db1 = get_falkor_db("localhost", 6379)
    db2 = get_falkor_db("localhost", 6379)
    assert db1 is db2
    assert build_count["n"] == 1

    db3 = get_falkor_db("otherhost", 6379)
    assert db3 is not db1
    assert build_count["n"] == 2


def test_two_falkor_stores_share_one_db_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two independently-constructed FalkorStore()s pointed at the same
    host/port must share the underlying FalkorDB connection."""
    from code_memory.graph import falkor_store as fs_mod

    monkeypatch.setattr(fs_mod, "_DBS", {}, raising=False)

    build_count = {"n": 0}

    class _FakeDb:
        def __init__(self, *a: Any, **kw: Any) -> None:
            build_count["n"] += 1
            self.connection = MagicMock()

        def select_graph(self, name: str) -> Any:
            return MagicMock()

    monkeypatch.setattr(fs_mod, "FalkorDB", _FakeDb)

    store1 = fs_mod.FalkorStore(host="localhost", port=6379, graph_name="g1")
    store2 = fs_mod.FalkorStore(host="localhost", port=6379, graph_name="g2")
    assert store1.db is store2.db
    assert build_count["n"] == 1


# ---------------------------------------------------------------------------
# 2. QdrantStore.close() / FalkorStore.close() — idempotent teardown
# ---------------------------------------------------------------------------


def test_qdrant_store_close_exists_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_memory.vector import qdrant_store as qs_mod

    monkeypatch.setattr(qs_mod, "_CLIENTS", {}, raising=False)

    close_calls = {"n": 0}

    class _FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def close(self) -> None:
            close_calls["n"] += 1

    monkeypatch.setattr(qs_mod, "QdrantClient", _FakeClient)

    store = qs_mod.QdrantStore(url="http://localhost:6333")
    assert hasattr(store, "close") and callable(store.close)
    store.close()
    store.close()  # second call must be a no-op, not raise
    assert close_calls["n"] <= 1


def test_qdrant_store_close_never_raises_when_client_lacks_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared/singleton client with no `.close()` (or one that raises)
    must not blow up teardown."""
    from code_memory.vector import qdrant_store as qs_mod

    monkeypatch.setattr(qs_mod, "_CLIENTS", {}, raising=False)

    class _FakeClientNoClose:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

    monkeypatch.setattr(qs_mod, "QdrantClient", _FakeClientNoClose)

    store = qs_mod.QdrantStore(url="http://localhost:6333")
    store.close()
    store.close()


def test_falkor_store_close_exists_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_memory.graph import falkor_store as fs_mod

    monkeypatch.setattr(fs_mod, "_DBS", {}, raising=False)

    close_calls = {"n": 0}

    class _FakeConnection:
        def close(self) -> None:
            close_calls["n"] += 1

    class _FakeDb:
        def __init__(self, *a: Any, **kw: Any) -> None:
            self.connection = _FakeConnection()

        def select_graph(self, name: str) -> Any:
            return MagicMock()

    monkeypatch.setattr(fs_mod, "FalkorDB", _FakeDb)

    store = fs_mod.FalkorStore(host="localhost", port=6379, graph_name="g1")
    assert hasattr(store, "close") and callable(store.close)
    store.close()
    store.close()
    assert close_calls["n"] <= 1


# ---------------------------------------------------------------------------
# 3. Pipeline as a context manager — closes owned sqlite stores, idempotent
# ---------------------------------------------------------------------------


class _FakeSqliteStore:
    """Stand-in for EpisodicStore / IngestStateStore — spies on close()."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _FakeVectorForCtx:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    def ensure_collection(self, name: str) -> None:
        pass


class _FakeGraphForCtx:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    def ensure_indexes(self) -> None:
        pass


def _patch_pipeline_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_memory.orchestrator import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "QdrantStore", _FakeVectorForCtx)
    monkeypatch.setattr(pipeline_mod, "FalkorStore", _FakeGraphForCtx)
    monkeypatch.setattr(pipeline_mod, "EpisodicStore", _FakeSqliteStore)
    monkeypatch.setattr(pipeline_mod, "IngestStateStore", _FakeSqliteStore)
    monkeypatch.setattr(pipeline_mod, "get_embedder", lambda: object())


def test_pipeline_is_a_context_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """`with Pipeline(project=...) as p:` must work without touching any
    real Qdrant/Falkor/sqlite backend."""
    _patch_pipeline_backends(monkeypatch)
    from code_memory.orchestrator.pipeline import Pipeline

    with Pipeline(project="watchd-test") as p:
        assert p is not None
        assert isinstance(p.episodic, _FakeSqliteStore)
        assert isinstance(p.state, _FakeSqliteStore)


def test_pipeline_close_closes_owned_sqlite_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pipeline_backends(monkeypatch)
    from code_memory.orchestrator.pipeline import Pipeline

    with Pipeline(project="watchd-test") as p:
        pass

    assert p.episodic.close_calls == 1
    assert p.state.close_calls == 1


def test_pipeline_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pipeline_backends(monkeypatch)
    from code_memory.orchestrator.pipeline import Pipeline

    pipe = Pipeline(project="watchd-test")
    pipe.close()
    pipe.close()  # second call must be a no-op

    assert pipe.episodic.close_calls == 1
    assert pipe.state.close_calls == 1


def test_pipeline_exit_closes_even_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """`__exit__` must run close() even when the `with` body raises, so a
    daemon wrapping each sync in `with Pipeline(...) as p:` never leaks a
    sqlite handle when a single ingest fails."""
    _patch_pipeline_backends(monkeypatch)
    from code_memory.orchestrator.pipeline import Pipeline

    pipe = Pipeline(project="watchd-test")
    with pytest.raises(RuntimeError):
        with pipe:
            raise RuntimeError("boom")

    assert pipe.episodic.close_calls == 1
    assert pipe.state.close_calls == 1


# ---------------------------------------------------------------------------
# 4. Per-project sqlite store reuse — process-level cache accessors
#
# REMOVED (architect ruling, option A over option B):
#
#   test_get_episodic_store_returns_same_instance_for_same_path
#   test_get_episodic_store_returns_distinct_instances_for_distinct_paths
#   test_get_episodic_store_only_constructs_once_per_path
#   test_get_ingest_state_store_returns_same_instance_for_same_path
#   test_get_ingest_state_store_returns_distinct_instances_for_distinct_paths
#
# These five tests encoded "option B": a process-wide singleton sqlite
# connection per db path (``get_episodic_store`` / `get_ingest_state_store`),
# reused across every ``Pipeline()``/``sync_repo()`` call to avoid reopening
# a handle each time. That design is REJECTED — it is broken by
# construction in the watchd daemon: ``EpisodicStore``/``IngestStateStore``
# open sqlite with the default ``check_same_thread=True``, but the daemon
# fires each watched root's sync from a brand-new ``threading.Timer``
# thread per debounce (see ``sync/watcher.py``: ``Debouncer.bump`` starts a
# fresh ``threading.Timer(self.window, self._fire)`` on every bump). The
# SECOND sync for a project therefore runs on a different OS thread than
# whichever thread first built the cached connection, and any use of that
# cached connection raises ``sqlite3.ProgrammingError: SQLite objects
# created in a thread can only be used in that same thread``. That failure
# was live in production: ``sync.py:132`` called
# ``get_ingest_state_store(cfg.episodic_db)`` for its standalone
# prior-state read, so every sync after the first for a given project
# crashed cross-thread once the daemon's per-fire-thread model kicked in.
#
# The accepted fix ("option A") is per-call, ``with``-scoped ownership
# instead of a cross-call cache: every ``Pipeline(...)`` construction in
# ``sync.py`` is wrapped in ``with Pipeline(project=slug) as pipe: ...``
# (opens + closes its own two sqlite stores on whichever thread runs that
# call, every call), and the standalone read at ``sync.py:132`` becomes
# ``with IngestStateStore(cfg.episodic_db) as state_store: prior =
# state_store.get(root_path)``. This is leak-free (see the Pipeline
# close() tests above/below) AND thread-safe (a fresh connection per call
# has no cross-thread lifetime to violate) without any store-level change.
# ``get_episodic_store`` / `get_ingest_state_store` are deleted (or
# hard-deprecated with a "not thread-safe, do not use" docstring) as part
# of that fix — see ``tests/test_sync_sqlite_lifecycle.py`` for the RED
# tests pinning the replacement behavior (context-manager protocol on both
# stores, the threaded production-path leak+correctness test, and the
# noop-path short-lived-connection pin).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. Full-ingest ThreadPoolExecutor must be shut down even on exception
# ---------------------------------------------------------------------------


def _build_bare_pipeline(tmp_path: Path) -> Any:
    """Construct a Pipeline via __new__ with mocked backends (no real
    Qdrant/Falkor/sqlite). Mirrors tests/test_graph_shadow_swap.py."""
    from code_memory.config import CONFIG
    from code_memory.orchestrator.pipeline import Pipeline

    mock_vector = MagicMock()
    mock_graph = MagicMock()
    mock_episodic = MagicMock()
    mock_state = MagicMock()
    mock_state.get.return_value = None
    mock_vector._inspect_collection.return_value = "hybrid"
    mock_vector.client = MagicMock()
    mock_vector.client.scroll.return_value = ([], None)

    pipe = Pipeline.__new__(Pipeline)
    pipe.slug = "watchd-exc-test"
    pipe.cfg = CONFIG.for_project("watchd-exc-test")
    pipe.skip_vectors = True
    pipe.embedder = MagicMock()
    pipe.vector = mock_vector
    pipe.graph = mock_graph
    pipe.episodic = mock_episodic
    pipe.state = mock_state
    pipe._active_code_collection = pipe.cfg.qdrant_code
    return pipe


def test_full_ingest_shuts_down_executor_when_walk_raises(tmp_path: Path) -> None:
    """A mid-walk exception (extractor crash, embed HTTP failure, etc.)
    must still shut down the upsert ThreadPoolExecutor — leaving it running
    leaks worker threads on every failed full ingest in a long-lived
    daemon that retries repeatedly."""
    from unittest.mock import patch

    from code_memory.orchestrator import pipeline as pipeline_mod

    pipe = _build_bare_pipeline(tmp_path)

    fake_executor = MagicMock()
    executor_factory = MagicMock(return_value=fake_executor)

    def _failing_walk(root: Path) -> Any:
        raise RuntimeError("simulated extractor crash")
        yield  # pragma: no cover - unreachable, keeps this a generator function

    with patch.object(pipeline_mod, "ThreadPoolExecutor", executor_factory):
        with patch.object(pipeline_mod, "Extractor") as MockExtractor:
            MockExtractor.return_value.walk.side_effect = _failing_walk
            with pytest.raises(RuntimeError, match="simulated extractor crash"):
                pipe._ingest_full(tmp_path, dry_run=False)

    fake_executor.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# 6. get_embedder() cold-start must be thread-safe (build exactly once)
# ---------------------------------------------------------------------------


def test_get_embedder_builds_exactly_once_under_concurrent_cold_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N threads racing to cold-start the embedder singleton must not each
    construct their own backend — that's N models loaded into a long-lived
    daemon process instead of one."""
    import code_memory.embed as embed_pkg

    embed_pkg.set_embedder_for_tests(None)
    monkeypatch.setattr(embed_pkg, "_SINGLETON", None, raising=False)
    monkeypatch.setenv(embed_pkg.ENV_DISABLE_CACHE, "1")  # skip the sqlite cache wrapper

    build_count = {"n": 0}
    build_lock = threading.Lock()

    class _SlowFakeEmbedder:
        def embed(self, texts: Any) -> Any:
            return []

        def embed_one(self, text: str) -> Any:
            return None

    def _slow_build_inner(backend: str) -> Any:
        with build_lock:
            build_count["n"] += 1
        # Widen the race window so a missing lock reliably produces >1 build.
        time.sleep(0.05)
        return _SlowFakeEmbedder(), "fake-model"

    monkeypatch.setattr(embed_pkg, "_build_inner_embedder", _slow_build_inner)

    results: list[Any] = []
    results_lock = threading.Lock()

    def _worker() -> None:
        e = embed_pkg.get_embedder()
        with results_lock:
            results.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert build_count["n"] == 1, f"expected exactly 1 build, got {build_count['n']}"
        assert len({id(r) for r in results}) == 1
    finally:
        embed_pkg.set_embedder_for_tests(None)


# ---------------------------------------------------------------------------
# 7. sync_repo must serialize concurrent calls for the same (root, project)
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path) -> str:
    import subprocess

    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t.test"],
        ["config", "user.name", "Test"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "a.py").write_text("x = 1\n")
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


def test_sync_repo_calls_single_flight_try_acquire_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sync_repo` must gate its ingest body behind
    `single_flight.try_acquire` / `release` for the (root, project) pair —
    otherwise two overlapping daemon-triggered syncs for the same repo can
    run the pipeline concurrently against the same sqlite/graph state."""
    from code_memory.sync import sync as sync_mod

    _init_git_repo(tmp_path)

    calls: list[str] = []
    # sync_mod.single_flight must exist as an attribute (module import) —
    # AttributeError here is the expected RED failure until it's wired in.
    real_try_acquire = sync_mod.single_flight.try_acquire
    real_release = sync_mod.single_flight.release

    def _recording_try_acquire(root: Path, project: str) -> bool:
        calls.append("try_acquire")
        return real_try_acquire(root, project)

    def _recording_release(root: Path, project: str) -> None:
        calls.append("release")
        real_release(root, project)

    monkeypatch.setattr(sync_mod.single_flight, "try_acquire", _recording_try_acquire)
    monkeypatch.setattr(sync_mod.single_flight, "release", _recording_release)
    monkeypatch.setattr(
        sync_mod,
        "_run_full_ingest",
        lambda root, slug, head, branch, store, *, publish: sync_mod.SyncResult(
            action="full_ingest", head_sha=head
        ),
    )

    sync_mod.sync_repo(tmp_path, project="watchd-lock-test", fetch=False)

    assert "try_acquire" in calls, "sync_repo must call single_flight.try_acquire"
    assert "release" in calls, "sync_repo must call single_flight.release"
    assert calls.index("try_acquire") < calls.index("release")


def test_sync_repo_skips_ingest_body_when_lock_already_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When another process/thread already holds the single-flight slot for
    this (root, project), a second sync_repo call must not run the ingest
    body — it should return promptly without touching Pipeline."""
    from code_memory.sync import single_flight
    from code_memory.sync import sync as sync_mod

    _init_git_repo(tmp_path)
    slug = "watchd-lock-held-test"

    ingest_called = {"n": 0}

    def _fake_full_ingest(root, slug, head, branch, store, *, publish):  # noqa: ANN001
        ingest_called["n"] += 1
        return sync_mod.SyncResult(action="full_ingest", head_sha=head)

    monkeypatch.setattr(sync_mod, "_run_full_ingest", _fake_full_ingest)

    acquired = single_flight.try_acquire(tmp_path, slug)
    assert acquired, "precondition: lock must be acquirable in test"
    try:
        result = sync_mod.sync_repo(tmp_path, project=slug, fetch=False)
    finally:
        single_flight.release(tmp_path, slug)

    assert ingest_called["n"] == 0, "ingest body must not run while the slot is held"
    assert result.action != "full_ingest"
    assert any(
        "lock" in n.lower() or "running" in n.lower() or "progress" in n.lower()
        for n in result.notes
    ), f"expected a skip note explaining the lock, got: {result.notes}"


def test_sync_repo_serializes_concurrent_calls_same_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two threads calling sync_repo on the SAME root at the same time must
    never both be inside the ingest body concurrently."""
    from code_memory.sync import sync as sync_mod

    _init_git_repo(tmp_path)
    slug = "watchd-concurrency-test"

    state_lock = threading.Lock()
    concurrent = {"n": 0, "max": 0}

    def _fake_full_ingest(root, slug_, head, branch, store, *, publish):  # noqa: ANN001
        with state_lock:
            concurrent["n"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["n"])
        time.sleep(0.05)
        with state_lock:
            concurrent["n"] -= 1
        return sync_mod.SyncResult(action="full_ingest", head_sha=head)

    monkeypatch.setattr(sync_mod, "_run_full_ingest", _fake_full_ingest)

    def _worker() -> None:
        sync_mod.sync_repo(tmp_path, project=slug, fetch=False)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert concurrent["max"] == 1, (
        f"expected at most 1 concurrent ingest body execution, saw {concurrent['max']}"
    )


# ---------------------------------------------------------------------------
# 8. Release-gate: no unbounded resource growth over many build+close cycles
# ---------------------------------------------------------------------------


def test_qdrant_singleton_client_count_stays_one_over_many_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 Pipeline-style QdrantStore() constructions in one process must
    resolve to exactly one underlying client — never N clients."""
    from code_memory.vector import qdrant_store as qs_mod

    monkeypatch.setattr(qs_mod, "_CLIENTS", {}, raising=False)

    build_count = {"n": 0}

    class _FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            build_count["n"] += 1

        def get_collections(self) -> Any:
            return MagicMock(collections=[])

    monkeypatch.setattr(qs_mod, "QdrantClient", _FakeClient)

    n_cycles = 200
    clients = set()
    for _ in range(n_cycles):
        store = qs_mod.QdrantStore(url="http://localhost:6333")
        clients.add(id(store.client))

    assert build_count["n"] == 1, (
        f"expected 1 client build across {n_cycles} stores, got {build_count['n']}"
    )
    assert len(clients) == 1


def test_pipeline_open_close_cycles_leave_no_orphaned_sqlite_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates the daemon pattern: build a Pipeline per sync_repo call,
    use it inside `with`, then discard it. Over 200 cycles every opened
    sqlite store must have been closed exactly once — no growth of
    unclosed handles."""
    from code_memory.orchestrator import pipeline as pipeline_mod

    opened = {"n": 0}
    closed = {"n": 0}

    class _TrackedSqliteStore:
        def __init__(self, *a: Any, **kw: Any) -> None:
            opened["n"] += 1
            self._closed_once = False

        def close(self) -> None:
            if self._closed_once:
                return
            self._closed_once = True
            closed["n"] += 1

    class _FakeVector:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def ensure_collection(self, name: str) -> None:
            pass

    class _FakeGraph:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def ensure_indexes(self) -> None:
            pass

    monkeypatch.setattr(pipeline_mod, "QdrantStore", _FakeVector)
    monkeypatch.setattr(pipeline_mod, "FalkorStore", _FakeGraph)
    monkeypatch.setattr(pipeline_mod, "EpisodicStore", _TrackedSqliteStore)
    monkeypatch.setattr(pipeline_mod, "IngestStateStore", _TrackedSqliteStore)
    monkeypatch.setattr(pipeline_mod, "get_embedder", lambda: object())

    n_cycles = 200
    for _ in range(n_cycles):
        with pipeline_mod.Pipeline(project="watchd-cycle-test"):
            pass

    assert opened["n"] == 2 * n_cycles, (
        f"expected {2 * n_cycles} store opens, got {opened['n']}"
    )
    assert closed["n"] == opened["n"], (
        f"leak detected: opened {opened['n']} sqlite stores but only "
        f"closed {closed['n']} — {opened['n'] - closed['n']} orphaned"
    )
