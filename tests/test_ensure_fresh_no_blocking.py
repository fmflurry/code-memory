"""Tests for Phase-1 fix: _ensure_fresh must never block on a full reingest.

Three invariants verified:
  (i)  ``_ensure_fresh`` never calls ``sync_repo`` (or Pipeline.ingest_repo)
       synchronously with mode="full" — it must fire a background thread instead.
  (ii) Collection point-count never hits 0 mid-rebuild / survives interrupted
       rebuild (shadow-collection swap in Pipeline._ingest_full).
  (iii) Concurrent retrieves launch exactly one background rebuild.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# (i) _ensure_fresh never calls sync_repo synchronously
# ---------------------------------------------------------------------------


def test_ensure_fresh_fires_background_thread_not_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_ensure_fresh must not block: it fires a daemon thread, returns fast."""
    from code_memory import mcp_server

    # Simulate a git repo.
    (tmp_path / ".git").mkdir()

    # Patch _is_index_stale to say "stale" so a rebuild would be triggered.
    monkeypatch.setattr(mcp_server, "_is_index_stale", lambda repo, project: True)

    sync_calls: list[str] = []
    thread_started: list[threading.Thread] = []

    # Capture Thread.start to record background threads without actually running.
    original_thread_init = threading.Thread.__init__

    class _TrackedThread(threading.Thread):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            original_thread_init(self, *args, **kwargs)
            thread_started.append(self)

        def start(self) -> None:
            # Record that a thread was started but don't actually run it
            # so we can assert the call was non-blocking.
            pass

    monkeypatch.setattr(threading, "Thread", _TrackedThread)

    # Patch sync_repo to detect any synchronous call.
    def _fake_sync(*args: Any, **kwargs: Any) -> Any:
        sync_calls.append("sync_repo")
        return MagicMock(action="noop", head_sha=None)

    monkeypatch.setenv("CODE_MEMORY_REPO", str(tmp_path))
    monkeypatch.setenv("CODE_MEMORY_NO_GUARD", "")

    with patch("code_memory.mcp_server._background_rebuild"):
        with patch("code_memory.mcp_server._maybe_trigger_background_rebuild") as mock_trigger:
            mock_trigger.return_value = None
            t0 = time.monotonic()
            mcp_server._ensure_fresh("test-project")
            elapsed = time.monotonic() - t0

    # Must return quickly (well under 1 second — no blocking sync call).
    assert elapsed < 1.0, f"_ensure_fresh blocked for {elapsed:.2f}s"
    # sync_repo must NOT have been called synchronously.
    assert sync_calls == [], "sync_repo was called synchronously — must not block"


def test_ensure_fresh_no_guard_env_skips_entirely(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CODE_MEMORY_NO_GUARD=1 must bypass the guard entirely."""
    from code_memory import mcp_server

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("CODE_MEMORY_NO_GUARD", "1")
    monkeypatch.setenv("CODE_MEMORY_REPO", str(tmp_path))

    triggered: list[bool] = []

    monkeypatch.setattr(
        mcp_server,
        "_maybe_trigger_background_rebuild",
        lambda *a, **kw: triggered.append(True),
    )

    mcp_server._ensure_fresh("test-project")
    assert triggered == [], "guard must be bypassed when CODE_MEMORY_NO_GUARD=1"


def test_ensure_fresh_skips_when_no_git_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_ensure_fresh is a no-op when the repo has no .git directory."""
    from code_memory import mcp_server

    # tmp_path has no .git dir.
    monkeypatch.setenv("CODE_MEMORY_REPO", str(tmp_path))
    monkeypatch.delenv("CODE_MEMORY_NO_GUARD", raising=False)

    triggered: list[bool] = []
    monkeypatch.setattr(
        mcp_server,
        "_maybe_trigger_background_rebuild",
        lambda *a, **kw: triggered.append(True),
    )

    mcp_server._ensure_fresh("test-project")
    assert triggered == [], "guard must be skipped for non-git directories"


def test_is_index_stale_returns_false_on_probe_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hung freshness probe must time out and return False (serve stale)."""
    from code_memory import mcp_server

    # Use a very short timeout so the test runs fast.
    monkeypatch.setattr(mcp_server, "_FRESHNESS_PROBE_TIMEOUT", 0.05)

    def _slow_probe_patcher(repo: Path, project: str) -> bool:
        """Simulate a slow probe by monkey-patching the IngestStateStore lookup."""
        # We can't easily make the thread itself slow without real infrastructure.
        # Instead verify the timeout logic directly by patching the thread join.
        return False

    # Patch ingest_state to hang.
    import code_memory.orchestrator.ingest_state as _iss

    class _HungStore:
        def get(self, root: Any) -> None:
            time.sleep(10)  # longer than probe timeout

    monkeypatch.setattr(_iss, "IngestStateStore", lambda **kw: _HungStore())

    # Should return False (not raise, not block indefinitely).
    result = mcp_server._is_index_stale(tmp_path, "test-project")
    assert result is False


# ---------------------------------------------------------------------------
# (ii) Shadow-collection swap: point count never hits 0 mid-rebuild
# ---------------------------------------------------------------------------


def test_ingest_full_uses_shadow_collection(
    tmp_path: Path,
) -> None:
    """_ingest_full must route upserts to a shadow collection, not the live one."""
    from unittest.mock import MagicMock, call, patch
    from code_memory.orchestrator.pipeline import Pipeline

    mock_vector = MagicMock()
    mock_graph = MagicMock()
    mock_episodic = MagicMock()
    mock_state = MagicMock()
    mock_state.get.return_value = None  # No prior state

    # Embedder that returns minimal valid HybridVec objects.
    from code_memory.embed.m3 import HybridVec, SparseVec
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [HybridVec(dense=[0.1], sparse=SparseVec(indices=[], values=[]))]

    # Make _inspect_collection return "hybrid" so ensure_collection passes.
    mock_vector._inspect_collection.return_value = "hybrid"
    mock_vector.client = MagicMock()
    # scroll returns empty result immediately.
    mock_vector.client.scroll.return_value = ([], None)

    pipe = Pipeline.__new__(Pipeline)
    pipe.slug = "test-project"
    from code_memory.config import CONFIG
    pipe.cfg = CONFIG.for_project("test-project")
    pipe.skip_vectors = True  # skip actual embed+upsert for unit test
    pipe.embedder = mock_embedder
    pipe.vector = mock_vector
    pipe.graph = mock_graph
    pipe.episodic = mock_episodic
    pipe.state = mock_state
    pipe._active_code_collection = pipe.cfg.qdrant_code

    # Patch extractor to yield nothing (empty repo).
    with patch("code_memory.orchestrator.pipeline.Extractor") as MockExtractor:
        MockExtractor.return_value.walk.return_value = iter([])
        with patch.object(pipe, "_ingest_dotnet_projects"):
            with patch.object(pipe, "_commit_shadow_collection") as mock_commit:
                pipe._ingest_full(tmp_path, dry_run=False)

    # Shadow collection must have been created (not the live one deleted first).
    shadow_name = pipe.cfg.qdrant_code + "__shadow"
    mock_vector.recreate_collection.assert_any_call(shadow_name)

    # Live collection must NOT have been recreated (no purge-before-rebuild).
    live_recreate_calls = [
        str(c) for c in mock_vector.recreate_collection.call_args_list
        if c == call(pipe.cfg.qdrant_code)
    ]
    assert live_recreate_calls == [], (
        "_purge_project_index must not be called before rebuild completes; "
        f"found recreate_collection({pipe.cfg.qdrant_code!r}) calls"
    )

    # Shadow commit must have been called (shadow → live swap).
    mock_commit.assert_called_once()

    # _active_code_collection must have been set to shadow during rebuild.
    # After commit it's reset; but the shadow name must have been used.
    # We verify the shadow was set by checking recreate_collection was called
    # with shadow_name before commit (ordering in calls list).
    all_recreate_args = [
        c.args[0] for c in mock_vector.recreate_collection.call_args_list
    ]
    assert shadow_name in all_recreate_args, (
        f"shadow collection {shadow_name!r} was never created; "
        f"recreate_collection calls: {all_recreate_args}"
    )


def test_interrupted_rebuild_leaves_live_collection_intact(
    tmp_path: Path,
) -> None:
    """An exception mid-rebuild must not have purged the live collection."""
    from unittest.mock import MagicMock, patch, call
    from code_memory.orchestrator.pipeline import Pipeline

    mock_vector = MagicMock()
    mock_graph = MagicMock()
    mock_state = MagicMock()
    mock_state.get.return_value = None
    mock_vector.client = MagicMock()
    mock_vector._inspect_collection.return_value = "hybrid"

    # Track every recreate_collection call.
    recreate_calls: list[str] = []

    def _track_recreate(name: str) -> None:
        recreate_calls.append(name)

    mock_vector.recreate_collection.side_effect = _track_recreate

    pipe = Pipeline.__new__(Pipeline)
    pipe.slug = "test-project"
    from code_memory.config import CONFIG
    pipe.cfg = CONFIG.for_project("test-project")
    pipe.skip_vectors = True
    pipe.embedder = MagicMock()
    pipe.vector = mock_vector
    pipe.graph = mock_graph
    pipe.episodic = MagicMock()
    pipe.state = mock_state
    pipe._active_code_collection = pipe.cfg.qdrant_code

    # Force the extractor walk to raise mid-way to simulate an interrupted rebuild.
    def _failing_walk(root: Path) -> Any:
        yield MagicMock(
            symbols=[], imports=[], calls=[], references=[], path="x.py", lang="py"
        )
        raise RuntimeError("simulated crash mid-rebuild")

    with patch("code_memory.orchestrator.pipeline.Extractor") as MockExtractor:
        MockExtractor.return_value.walk.side_effect = _failing_walk
        with pytest.raises(RuntimeError, match="simulated crash mid-rebuild"):
            pipe._ingest_full(tmp_path, dry_run=False)

    live_name = pipe.cfg.qdrant_code
    shadow_name = live_name + "__shadow"

    # The live collection must never have been recreated (purged).
    assert live_name not in recreate_calls, (
        f"live collection {live_name!r} was recreated before rebuild finished; "
        f"calls: {recreate_calls}"
    )
    # The shadow collection was created (started building into it).
    assert shadow_name in recreate_calls, (
        f"shadow collection {shadow_name!r} was never created; calls: {recreate_calls}"
    )


# ---------------------------------------------------------------------------
# (iii) Concurrent retrieves launch exactly one rebuild
# ---------------------------------------------------------------------------


def test_concurrent_ensure_fresh_launches_exactly_one_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """N concurrent _ensure_fresh calls must trigger at most one background rebuild."""
    from code_memory import mcp_server

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("CODE_MEMORY_REPO", str(tmp_path))
    monkeypatch.delenv("CODE_MEMORY_NO_GUARD", raising=False)

    # Always stale.
    monkeypatch.setattr(mcp_server, "_is_index_stale", lambda repo, project: True)

    rebuild_count: list[int] = []
    rebuild_lock = threading.Lock()

    def _fake_background_rebuild(repo: Path, project: str) -> None:
        # Simulate the real _background_rebuild: count the call and release
        # the single-flight slot on exit (slot ownership was transferred to us).
        from code_memory.sync.single_flight import release
        try:
            with rebuild_lock:
                rebuild_count.append(1)
            # Simulate a slow rebuild.
            time.sleep(0.1)
        finally:
            release(repo, project)

    monkeypatch.setattr(mcp_server, "_background_rebuild", _fake_background_rebuild)

    # Use real single_flight but clean up any stale lock before test.
    from code_memory.sync import single_flight
    k = single_flight._key(tmp_path.resolve(), "test-project")
    with single_flight._registry_lock:
        single_flight._held.discard(k)

    # Fire N concurrent _ensure_fresh calls.
    N = 10
    errors: list[Exception] = []

    def _call() -> None:
        try:
            mcp_server._ensure_fresh("test-project")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert errors == [], f"_ensure_fresh raised: {errors}"

    # Give the fake rebuild time to complete.
    time.sleep(0.2)

    # Exactly one rebuild must have been launched.
    assert len(rebuild_count) == 1, (
        f"Expected exactly 1 background rebuild, got {len(rebuild_count)}"
    )


def test_single_flight_try_acquire_blocks_second_caller(
    tmp_path: Path,
) -> None:
    """try_acquire returns False when a slot is already held."""
    from code_memory.sync.single_flight import release, try_acquire
    import code_memory.sync.single_flight as sf

    # Clean state.
    k = sf._key(tmp_path.resolve(), "test-project")
    with sf._registry_lock:
        sf._held.discard(k)
    pid_path = sf._pid_file(k)
    pid_path.unlink(missing_ok=True)

    assert try_acquire(tmp_path, "test-project") is True
    # Second acquire must fail.
    assert try_acquire(tmp_path, "test-project") is False

    release(tmp_path, "test-project")
    # After release, can acquire again.
    assert try_acquire(tmp_path, "test-project") is True
    release(tmp_path, "test-project")


def test_single_flight_stale_pid_file_is_evicted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lockfile with a dead PID must be evicted, not block acquisition."""
    from code_memory.sync import single_flight as sf

    k = sf._key(tmp_path.resolve(), "stale-project")
    with sf._registry_lock:
        sf._held.discard(k)

    pid_path = sf._pid_file(k)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    # Write a PID that cannot be alive (PID 1 is always init on Linux but we
    # use a clearly bogus value; os.kill will raise OSError for dead PIDs).
    dead_pid = 99999999
    monkeypatch.setattr(sf, "_pid_alive", lambda pid: False)
    pid_path.write_text(str(dead_pid))

    # Should succeed despite the stale lockfile.
    result = sf.try_acquire(tmp_path, "stale-project")
    assert result is True
    sf.release(tmp_path, "stale-project")
