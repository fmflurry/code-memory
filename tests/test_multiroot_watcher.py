"""RED tests for Phase 2: a single-process, many-roots watcher daemon.

Nothing in this file exists yet: ``MultiRootWatcher``, ``run_daemon``, and
``write_daemon_state`` must be added to ``src/code_memory/sync/watcher.py``
ALONGSIDE the existing single-repo ``Watcher``/``run_foreground`` (this file
never modifies those). Every test below must fail until the GREEN
implementation lands — via ``AttributeError`` on the missing symbol, never a
bug in this test file.

Target API (see the GREEN spec returned to the orchestrator for the full
contract):

* ``MultiRootWatcher(*, observer=None, debounce=DEFAULT_DEBOUNCE)`` — one
  ``watchdog`` ``Observer`` (or an injected test double) plus three
  per-root maps keyed by *resolved* ``Path``: ``_watches`` (root ->
  ``ObservedWatch``), ``_debouncers`` (root -> ``Debouncer``), ``_handlers``
  (root -> the per-root event handler instance).
* ``add_root(root, slug)`` — idempotent; builds a handler bound to that
  root's own ``_default_exclude`` + its own ``Debouncer`` whose trigger
  calls ``sync_repo(root, project=slug, trigger="watchd")``.
* ``remove_root(root)`` — cancels the debouncer, unschedules the exact
  stored ``ObservedWatch``, empties all three maps for that key. No-op if
  absent.
* ``reconcile(desired: dict[Path, str])`` — delta only: removes roots gone
  from ``desired``, adds new ones, leaves unchanged roots completely
  untouched (same ``ObservedWatch`` identity, no re-schedule). Skips any
  root that fails ``assert_safe_watch_root`` or for which
  ``is_non_persistent_watch_dir`` is true (both referenced as
  ``watcher_mod.assert_safe_watch_root`` / ``watcher_mod.is_non_persistent_watch_dir``
  so tests can monkeypatch them directly on the watcher module).
* ``write_daemon_state(watched_roots)`` — writes ``watchd_state_path()``
  as JSON: ``{"pid": int, "watched_roots": [sorted resolved-path strings],
  "ts": float}``.
* ``run_daemon(*, observer=None, stop_event=None, reconcile_debounce=1.5,
  poll_interval=0.5, on_reconcile=None)`` — blocking; does an initial
  ``reconcile(registry.load())``, self-watches the registry file's parent
  dir (filtered to the registry filename) with a dedicated reconcile
  ``Debouncer``, writes daemon state after every reconcile (initial and
  registry-triggered), and calls ``on_reconcile(watcher)`` after each
  reconcile when provided (test hook only — production callers pass
  nothing). Returns when ``stop_event`` is set, cancelling every
  debouncer and stopping/joining the observer.
"""

from __future__ import annotations

import json
import os
import threading
import time
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from code_memory.config import watchd_state_path
from code_memory.sync import registry as registry_mod
from code_memory.sync import watcher as watcher_mod
from code_memory.sync.safety import UnsafeWatchRootError
from code_memory.sync.watcher import Debouncer

# ---------------------------------------------------------------------------
# Test doubles — fast, deterministic, no real OS file-watching threads.
# ---------------------------------------------------------------------------


class _FakeObservedWatch:
    """Stand-in for watchdog.observers.api.ObservedWatch.

    A fresh instance per ``schedule()`` call gives each stored watch its
    own identity, so tests can assert "same object" for untouched roots
    and "different object" for re-scheduled ones.
    """

    def __init__(self, path: str, recursive: bool) -> None:
        self.path = path
        self.recursive = recursive


class _FakeObserver:
    """Test double for watchdog.observers.api.BaseObserver.

    Records every schedule/unschedule call so the leak-gate test can
    assert call-count symmetry without spinning up real OS watcher
    threads (which would make a 200+ cycle loop slow and platform-flaky).
    """

    def __init__(self) -> None:
        self.schedule_calls: list[tuple[Any, str]] = []
        self.unschedule_calls: list[_FakeObservedWatch] = []
        self.started = False
        self.stopped = False
        self.joined = False

    def schedule(
        self,
        handler: Any,
        path: str,
        *,
        recursive: bool = True,
        event_filter: Any = None,
    ) -> _FakeObservedWatch:
        watch = _FakeObservedWatch(path, recursive)
        self.schedule_calls.append((handler, path))
        return watch

    def unschedule(self, watch: _FakeObservedWatch) -> None:
        self.unschedule_calls.append(watch)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


def _patch_sync_repo(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def _fake_sync_repo(root: Any, *, project: Any, trigger: Any) -> None:
        calls.append((Path(root), project, trigger))

    monkeypatch.setattr(watcher_mod, "sync_repo", _fake_sync_repo)
    return calls


def _touch_event(path: Path) -> Any:
    from watchdog.events import FileModifiedEvent

    return FileModifiedEvent(str(path))


def _new_watcher(observer: Any, **kw: Any) -> Any:
    """AttributeError here is the expected RED failure until implemented."""
    return watcher_mod.MultiRootWatcher(observer=observer, **kw)


# ---------------------------------------------------------------------------
# 1. add_root: registers a watch; idempotent re-add doesn't double-schedule
# ---------------------------------------------------------------------------


def test_add_root_registers_watch_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)
    root = tmp_path / "repo-a"
    root.mkdir()

    watcher.add_root(root, "repo-a-slug")

    resolved = root.resolve()
    assert resolved in watcher._watches
    assert resolved in watcher._debouncers
    assert resolved in watcher._handlers
    assert isinstance(watcher._watches[resolved], _FakeObservedWatch)
    assert len(observer.schedule_calls) == 1

    watcher.add_root(root, "repo-a-slug")  # idempotent re-add

    assert len(observer.schedule_calls) == 1, (
        "re-adding an already-watched root must not double-schedule"
    )


# ---------------------------------------------------------------------------
# 2. Per-root debounce isolation
# ---------------------------------------------------------------------------


def test_event_on_one_root_never_triggers_another_roots_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer, debounce=0.03)
    root_a = tmp_path / "repo-a"
    root_a.mkdir()
    root_b = tmp_path / "repo-b"
    root_b.mkdir()

    watcher.add_root(root_a, "slug-a")
    watcher.add_root(root_b, "slug-b")

    handler_a = watcher._handlers[root_a.resolve()]
    handler_a.on_any_event(_touch_event(root_a / "file.py"))

    time.sleep(0.3)

    assert calls == [(root_a.resolve(), "slug-a", "watchd")], (
        f"expected exactly one sync for root A, got {calls}"
    )


# ---------------------------------------------------------------------------
# 3. remove_root: cancels debouncer, unschedules exact ObservedWatch, empties maps
# ---------------------------------------------------------------------------


def test_remove_root_cancels_debouncer_and_unschedules_exact_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)
    root = tmp_path / "repo"
    root.mkdir()
    watcher.add_root(root, "slug")
    resolved = root.resolve()

    debouncer = watcher._debouncers[resolved]
    cancel_calls = {"n": 0}
    real_cancel = debouncer.cancel

    def _spy_cancel() -> None:
        cancel_calls["n"] += 1
        real_cancel()

    monkeypatch.setattr(debouncer, "cancel", _spy_cancel)
    stored_watch = watcher._watches[resolved]

    watcher.remove_root(root)

    assert cancel_calls["n"] == 1
    assert observer.unschedule_calls == [stored_watch]
    assert resolved not in watcher._watches
    assert resolved not in watcher._debouncers
    assert resolved not in watcher._handlers

    # No-op when the key is absent — must not raise, must not unschedule again.
    watcher.remove_root(root)
    assert observer.unschedule_calls == [stored_watch]


# ---------------------------------------------------------------------------
# 4. reconcile: delta only; unchanged roots keep the same ObservedWatch
# ---------------------------------------------------------------------------


def test_reconcile_delta_only_leaves_unchanged_roots_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)
    keep = tmp_path / "keep"
    keep.mkdir()
    gone = tmp_path / "gone"
    gone.mkdir()
    fresh = tmp_path / "fresh"
    fresh.mkdir()

    watcher.reconcile({keep: "keep-slug", gone: "gone-slug"})
    watch_keep_before = watcher._watches[keep.resolve()]
    schedule_count_before = len(observer.schedule_calls)

    watcher.reconcile({keep: "keep-slug", fresh: "fresh-slug"})

    assert gone.resolve() not in watcher._watches
    assert fresh.resolve() in watcher._watches
    assert keep.resolve() in watcher._watches
    assert watcher._watches[keep.resolve()] is watch_keep_before, (
        "unchanged root must keep the identical ObservedWatch object — "
        "reconcile must never do a full teardown/rebuild"
    )
    assert len(observer.schedule_calls) == schedule_count_before + 1, (
        "only the newly-added root should trigger a new observer.schedule() call"
    )


# ---------------------------------------------------------------------------
# 5. reconcile: skip unsafe / ephemeral roots
# ---------------------------------------------------------------------------


def test_reconcile_skips_root_that_fails_safety_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    safe = tmp_path / "safe"
    safe.mkdir()

    def _fake_assert_safe(root: Any) -> Path:
        resolved = Path(root).resolve()
        if resolved == unsafe.resolve():
            raise UnsafeWatchRootError("refusing unsafe root in test")
        return resolved

    monkeypatch.setattr(watcher_mod, "assert_safe_watch_root", _fake_assert_safe)

    watcher.reconcile({unsafe: "unsafe-slug", safe: "safe-slug"})

    assert unsafe.resolve() not in watcher._watches
    assert safe.resolve() in watcher._watches


def test_reconcile_skips_non_persistent_watch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)
    ephemeral = tmp_path / "eph"
    ephemeral.mkdir()
    safe = tmp_path / "safe"
    safe.mkdir()

    monkeypatch.setattr(
        watcher_mod,
        "is_non_persistent_watch_dir",
        lambda p: Path(p).resolve() == ephemeral.resolve(),
    )

    watcher.reconcile({ephemeral: "eph-slug", safe: "safe-slug"})

    assert ephemeral.resolve() not in watcher._watches
    assert safe.resolve() in watcher._watches


# ---------------------------------------------------------------------------
# 6. Registry-driven: run_daemon picks up a new root added to the registry
# ---------------------------------------------------------------------------


def test_run_daemon_picks_up_root_added_to_registry_after_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _patch_sync_repo(monkeypatch)

    existing = tmp_path / "existing-repo"
    existing.mkdir()
    registry_mod.add(existing, "existing-slug")

    new_root = tmp_path / "new-repo"
    new_root.mkdir()

    stop_event = threading.Event()
    picked_up = threading.Event()
    seen_roots: set[Path] = set()

    def _on_reconcile(watcher: Any) -> None:
        seen_roots.update(watcher._watches.keys())
        if new_root.resolve() in watcher._watches:
            picked_up.set()

    thread = threading.Thread(
        target=watcher_mod.run_daemon,
        kwargs={
            "stop_event": stop_event,
            "reconcile_debounce": 0.05,
            "poll_interval": 0.02,
            "on_reconcile": _on_reconcile,
        },
        daemon=True,
    )
    thread.start()
    try:
        # Let the daemon complete its initial reconcile and install the
        # registry self-watch before we mutate the registry file.
        time.sleep(0.5)
        registry_mod.add(new_root, "new-slug")
        assert picked_up.wait(5.0), (
            f"daemon never picked up the newly-registered root; saw {seen_roots}"
        )
    finally:
        stop_event.set()
        thread.join(timeout=3)
        assert not thread.is_alive(), "run_daemon must exit promptly once stop_event is set"


# ---------------------------------------------------------------------------
# 7. State file: pid + sorted watched-root set
# ---------------------------------------------------------------------------


def test_write_daemon_state_contains_pid_and_sorted_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root_b = tmp_path / "repo-b"
    root_b.mkdir()
    root_a = tmp_path / "repo-a"
    root_a.mkdir()

    watcher_mod.write_daemon_state([root_b.resolve(), root_a.resolve()])

    state_path = watchd_state_path()
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["pid"] == os.getpid()
    assert state["watched_roots"] == sorted(
        [str(root_a.resolve()), str(root_b.resolve())]
    )
    assert isinstance(state["ts"], (int, float))
    assert state["ts"] > 0


# ---------------------------------------------------------------------------
# 8. LEAK GATE (release blocker) — 200+ add/remove cycles, zero growth
# ---------------------------------------------------------------------------


def test_add_remove_cycles_leave_no_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)
    root = tmp_path / "repo"
    root.mkdir()

    created_debouncers: list[Debouncer] = []
    real_debouncer_init = Debouncer.__init__

    def _tracking_init(self: Debouncer, window: float, flush: Any) -> None:
        created_debouncers.append(self)
        real_debouncer_init(self, window, flush)

    monkeypatch.setattr(Debouncer, "__init__", _tracking_init)

    cancel_calls = {"n": 0}
    real_cancel = Debouncer.cancel

    def _tracking_cancel(self: Debouncer) -> None:
        cancel_calls["n"] += 1
        real_cancel(self)

    monkeypatch.setattr(Debouncer, "cancel", _tracking_cancel)

    baseline_threads = threading.active_count()
    tracemalloc.start()
    snapshot_before = tracemalloc.take_snapshot()

    n_cycles = 200
    for i in range(n_cycles):
        watcher.add_root(root, f"slug-{i}")
        watcher.remove_root(root)

    # Also drive the same churn through reconcile (add-then-remove in one
    # pass each time), matching the real daemon's registry-driven path.
    for i in range(n_cycles):
        watcher.reconcile({root: f"slug-{i}"})
        watcher.reconcile({})

    snapshot_after = tracemalloc.take_snapshot()
    tracemalloc.stop()

    assert watcher._watches == {}
    assert watcher._debouncers == {}
    assert watcher._handlers == {}
    assert len(observer.schedule_calls) == len(observer.unschedule_calls), (
        "schedule/unschedule call counts diverged — a dangling ObservedWatch leaked"
    )
    assert cancel_calls["n"] == len(created_debouncers), (
        f"created {len(created_debouncers)} Debouncers but only cancelled "
        f"{cancel_calls['n']} — {len(created_debouncers) - cancel_calls['n']} leaked"
    )

    final_threads = threading.active_count()
    assert final_threads <= baseline_threads + 2, (
        f"thread count grew from {baseline_threads} to {final_threads} over "
        f"{2 * n_cycles} add/remove cycles"
    )

    top_stats = snapshot_after.compare_to(snapshot_before, "lineno")
    growth = sum(stat.size_diff for stat in top_stats if stat.size_diff > 0)
    assert growth < 2_000_000, (
        f"tracemalloc measured {growth} bytes of net growth over "
        f"{2 * n_cycles} add/remove cycles"
    )


def test_real_observer_emitter_count_returns_to_baseline_after_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Supplementary leak check with a real watchdog Observer: emitter
    threads (one per scheduled watch) must not accumulate across repeated
    add/remove cycles against distinct roots."""
    _patch_sync_repo(monkeypatch)
    from watchdog.observers import Observer

    observer = Observer()
    observer.start()
    try:
        watcher = _new_watcher(observer)
        baseline = len(observer.emitters)

        n_cycles = 25
        for i in range(n_cycles):
            root = tmp_path / f"repo-{i}"
            root.mkdir()
            watcher.add_root(root, f"slug-{i}")
            watcher.remove_root(root)

        assert len(observer.emitters) == baseline, (
            f"emitter count grew from {baseline} to {len(observer.emitters)} "
            f"over {n_cycles} add/remove cycles against a real Observer"
        )
    finally:
        observer.stop()
        observer.join(timeout=3)
