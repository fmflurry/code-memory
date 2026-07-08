"""RED tests pinning fixes for review-found daemon-safety bugs.

Every test in this file must fail today (before the GREEN implementation
lands) and pass once the fixes below are made to
``src/code_memory/sync/watcher.py`` and ``src/code_memory/sync/registry.py``.
This file is additive — it never modifies ``tests/test_multiroot_watcher.py``
or ``tests/test_watch_registry.py``, and duplicates the small set of test
doubles it needs from those files rather than importing them, so it stays
independently runnable.

Bugs pinned (see the GREEN spec returned alongside this file for the full
contract each test enforces):

1. HIGH — a reconcile triggered by SIGHUP (or the registry self-watch) must
   never propagate an exception and kill the daemon. Both paths must route
   through one guarded helper, pinned here as ``watcher_mod._safe_reconcile``.
2. HIGH — ``write_daemon_state``'s root snapshot must never read
   ``MultiRootWatcher._watches`` unlocked. Pinned here as a new public
   thread-safe accessor ``MultiRootWatcher.watched_roots() -> list[Path]``.
3. MEDIUM — ``write_daemon_state`` must write atomically (temp file +
   ``os.replace``), never a direct in-place ``write_text``.
4. MEDIUM — ``registry._read_raw`` / ``registry.load`` must survive invalid
   UTF-8 bytes on disk (currently only ``OSError`` is caught around
   ``read_text``; a ``UnicodeDecodeError`` propagates today).
5. MEDIUM — ``MultiRootWatcher.reconcile`` must detect a slug change for a
   root whose *path* is unchanged and re-bind its handler/debouncer to the
   new slug, instead of silently keeping the stale closure.
6. MEDIUM — ``MultiRootWatcher.watch_registry`` must not leak the prior
   registry debouncer/watch on re-entry: it must cancel the old debouncer
   and unschedule the old watch before installing the new pair.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from code_memory import config
from code_memory.config import watchd_state_path
from code_memory.sync import registry
from code_memory.sync import registry as registry_mod
from code_memory.sync import watcher as watcher_mod

# ---------------------------------------------------------------------------
# Test doubles (duplicated from test_multiroot_watcher.py on purpose — see
# module docstring for why this file doesn't import them).
# ---------------------------------------------------------------------------


class _FakeObservedWatch:
    def __init__(self, path: str, recursive: bool) -> None:
        self.path = path
        self.recursive = recursive


class _FakeObserver:
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
    return watcher_mod.MultiRootWatcher(observer=observer, **kw)


def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


# ---------------------------------------------------------------------------
# 1. HIGH: guarded reconcile — SIGHUP and registry-watch paths never crash
#    the daemon on an internal exception.
# ---------------------------------------------------------------------------


def test_safe_reconcile_helper_swallows_registry_load_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct unit test of the shared guarded helper.

    ``watcher_mod._safe_reconcile`` doesn't exist yet -> AttributeError is
    the expected RED failure. Once implemented, it must swallow an
    exception raised by ``registry.load()`` and never propagate it.
    """
    _isolate_registry(tmp_path, monkeypatch)
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)

    def _boom() -> dict[str, Any]:
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(registry_mod, "load", _boom)

    # Must not raise.
    watcher_mod._safe_reconcile(watcher)


def test_safe_reconcile_helper_swallows_watcher_reconcile_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)

    def _boom(desired: Any) -> None:
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(watcher, "reconcile", _boom)

    # Must not raise.
    watcher_mod._safe_reconcile(watcher)


def test_safe_reconcile_helper_swallows_write_daemon_state_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)

    def _boom(watched_roots: Any) -> None:
        raise RuntimeError("state write exploded")

    monkeypatch.setattr(watcher_mod, "write_daemon_state", _boom)

    # Must not raise.
    watcher_mod._safe_reconcile(watcher)


def test_sighup_handler_routes_through_safe_reconcile_and_never_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the SIGHUP handler installed by ``run_daemon`` must be the
    same guarded path as the registry self-watch, so a reconcile failure at
    SIGHUP time never kills the daemon process.

    ``signal.signal`` is patched so this works regardless of which thread
    ``run_daemon`` executes in under test (the real call requires the main
    thread and is already best-effort/caught in production code).
    """
    _isolate_registry(tmp_path, monkeypatch)
    _patch_sync_repo(monkeypatch)

    captured_handlers: dict[int, Any] = {}

    def _fake_signal(sig: int, handler: Any) -> None:
        captured_handlers[sig] = handler

    monkeypatch.setattr(watcher_mod.signal, "signal", _fake_signal)

    observer = _FakeObserver()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=watcher_mod.run_daemon,
        kwargs={
            "observer": observer,
            "stop_event": stop_event,
            "reconcile_debounce": 0.05,
            "poll_interval": 0.02,
        },
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.time() + 3.0
        while signal.SIGHUP not in captured_handlers and time.time() < deadline:
            time.sleep(0.02)
        assert signal.SIGHUP in captured_handlers, (
            "run_daemon never installed a SIGHUP handler via signal.signal"
        )

        def _boom() -> dict[str, Any]:
            raise RuntimeError("boom triggered via SIGHUP")

        monkeypatch.setattr(registry_mod, "load", _boom)

        handler = captured_handlers[signal.SIGHUP]
        # Real signal handlers are invoked as handler(signum, frame); the
        # daemon's registered callable must tolerate being called either
        # way and, crucially, must not raise even though registry.load()
        # explodes underneath it.
        handler(signal.SIGHUP, None)
    finally:
        stop_event.set()
        thread.join(timeout=3)
        assert not thread.is_alive(), "run_daemon must still exit promptly after a SIGHUP reconcile failure"


# ---------------------------------------------------------------------------
# 2. HIGH: MultiRootWatcher.watched_roots() — thread-safe snapshot accessor.
# ---------------------------------------------------------------------------


def test_watched_roots_returns_stable_list_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)
    root = tmp_path / "repo"
    root.mkdir()
    watcher.add_root(root, "slug")

    snapshot = watcher.watched_roots()
    assert snapshot == [root.resolve()]

    # Mutating the watcher after the fact must not retroactively change an
    # already-returned snapshot (it must be a copy, not a live view).
    root2 = tmp_path / "repo2"
    root2.mkdir()
    watcher.add_root(root2, "slug2")
    assert snapshot == [root.resolve()]


def test_watched_roots_concurrent_with_add_remove_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort concurrency test: hammer add_root/remove_root on one
    thread while reading watched_roots() on another. Must never raise
    (pins the ``_lock``-guarded snapshot, guarding against
    'dictionary changed size during iteration')."""
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)
    roots = []
    for i in range(20):
        r = tmp_path / f"repo-{i}"
        r.mkdir()
        roots.append(r)

    stop = threading.Event()
    errors: list[Exception] = []

    def _churn() -> None:
        i = 0
        while not stop.is_set():
            root = roots[i % len(roots)]
            watcher.add_root(root, f"slug-{i}")
            watcher.remove_root(root)
            i += 1

    def _reader() -> None:
        deadline = time.time() + 1.0
        try:
            while time.time() < deadline:
                watcher.watched_roots()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    churner = threading.Thread(target=_churn, daemon=True)
    reader = threading.Thread(target=_reader, daemon=True)
    churner.start()
    reader.start()
    reader.join(timeout=5)
    stop.set()
    churner.join(timeout=5)

    assert errors == [], f"watched_roots() raised under concurrent add/remove: {errors}"


def test_safe_reconcile_persists_state_via_watched_roots_accessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_safe_reconcile`` (used for both the registry-watch and SIGHUP
    paths) must snapshot roots via the thread-safe ``watched_roots()``
    accessor rather than reading ``MultiRootWatcher._watches`` directly."""
    _isolate_registry(tmp_path, monkeypatch)
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)
    root = tmp_path / "repo"
    root.mkdir()
    registry_mod.add(root, "slug")

    calls: list[bool] = []
    real_watched_roots = watcher_mod.MultiRootWatcher.watched_roots

    def _spy(self: Any) -> list[Path]:
        calls.append(True)
        return real_watched_roots(self)

    monkeypatch.setattr(watcher_mod.MultiRootWatcher, "watched_roots", _spy)

    watcher_mod._safe_reconcile(watcher)

    assert calls, "_safe_reconcile must call watched_roots() to build the state-file snapshot"


# ---------------------------------------------------------------------------
# 3. MEDIUM: write_daemon_state must be atomic (temp file + os.replace).
# ---------------------------------------------------------------------------


def test_write_daemon_state_persists_via_os_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    root = tmp_path / "repo"
    root.mkdir()

    replace_calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def _spy_replace(src: Any, dst: Any) -> None:
        replace_calls.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(watcher_mod.os, "replace", _spy_replace)

    watcher_mod.write_daemon_state([root.resolve()])

    state_path = watchd_state_path()
    assert len(replace_calls) == 1, (
        "write_daemon_state must persist via os.replace (temp file + atomic "
        f"rename), not a direct write; observed {len(replace_calls)} os.replace calls"
    )
    assert replace_calls[0][1] == state_path

    # The state file must contain complete, parseable JSON immediately
    # after the call — never a half-written temp artifact.
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["watched_roots"] == [str(root.resolve())]


# ---------------------------------------------------------------------------
# 4. MEDIUM: registry.load() must survive invalid UTF-8 bytes on disk.
# ---------------------------------------------------------------------------


def test_load_invalid_utf8_returns_empty_dict_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    path = config.watch_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lone continuation/start bytes that are not valid UTF-8 under strict
    # decoding.
    path.write_bytes(b'{"root": {"slug": "\xff\xfe\x80\x81invalid"}}')

    # Must not raise UnicodeDecodeError; must degrade to the same
    # self-healing {} contract as corrupt/truncated JSON.
    assert registry.load() == {}


def test_read_raw_invalid_utf8_returns_empty_dict_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_registry(tmp_path, monkeypatch)
    path = config.watch_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00\x01not-utf8-at-all\x80")

    assert registry._read_raw(path) == {}


# ---------------------------------------------------------------------------
# 5. MEDIUM: reconcile must honor slug drift for an unchanged root path.
# ---------------------------------------------------------------------------


def test_reconcile_rebinds_handler_when_slug_changes_for_same_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer, debounce=0.03)
    root = tmp_path / "repo"
    root.mkdir()

    watcher.reconcile({root: "old"})
    watcher.reconcile({root: "new"})

    handler = watcher._handlers[root.resolve()]
    handler.on_any_event(_touch_event(root / "file.py"))
    time.sleep(0.3)

    assert calls == [(root.resolve(), "new", "watchd")], (
        f"expected the post-drift sync to be bound to slug 'new', got {calls}"
    )


# ---------------------------------------------------------------------------
# 6. MEDIUM: watch_registry re-entry must not leak the prior debouncer/watch.
# ---------------------------------------------------------------------------


def test_watch_registry_reentry_cancels_and_unschedules_prior_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sync_repo(monkeypatch)
    observer = _FakeObserver()
    watcher = _new_watcher(observer)
    registry_path = tmp_path / "registry.json"

    watcher.watch_registry(registry_path, debounce=0.05, on_change=lambda: None)

    first_debouncer = watcher._registry_debouncer
    first_watch = watcher._registry_watch
    assert first_debouncer is not None
    assert first_watch is not None

    cancel_calls = {"n": 0}
    real_cancel = first_debouncer.cancel

    def _spy_cancel() -> None:
        cancel_calls["n"] += 1
        real_cancel()

    monkeypatch.setattr(first_debouncer, "cancel", _spy_cancel)

    watcher.watch_registry(registry_path, debounce=0.05, on_change=lambda: None)

    assert cancel_calls["n"] == 1, (
        "re-entering watch_registry must cancel the prior registry debouncer"
    )
    assert observer.unschedule_calls == [first_watch], (
        "re-entering watch_registry must unschedule the prior registry watch"
    )
    assert watcher._registry_debouncer is not first_debouncer
    assert watcher._registry_watch is not first_watch
