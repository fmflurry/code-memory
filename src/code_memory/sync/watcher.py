"""Cross-platform filesystem watcher.

Uses ``watchdog`` (FSEvents on macOS, inotify on Linux, ReadDirectoryChangesW
on Windows) when available; otherwise falls back to mtime polling so the
feature degrades gracefully even when the optional dep is missing.

The watcher debounces bursts (e.g. ``git checkout`` touches many files at
once) and triggers a single ``sync_repo`` per quiet period. Excluded paths
include ``.git`` and any directory the extractor's gitignore filter drops.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .sync import SyncResult, sync_repo

log = logging.getLogger("codememory.watcher")

DEFAULT_DEBOUNCE = 2.0
DEFAULT_POLL_INTERVAL = 5.0

ExcludeFn = Callable[[Path], bool]


def _default_exclude(repo: Path) -> ExcludeFn:
    git_dir = (repo / ".git").resolve()
    data_dir = (repo / "data").resolve()
    venv_dir = (repo / ".venv").resolve()
    node_dir = (repo / "node_modules").resolve()

    def exclude(p: Path) -> bool:
        try:
            r = p.resolve()
        except OSError:
            return True
        for blocked in (git_dir, data_dir, venv_dir, node_dir):
            try:
                r.relative_to(blocked)
                return True
            except ValueError:
                continue
        return False

    return exclude


class Debouncer:
    """Coalesce bursts; fire ``flush`` once activity quiets for ``window`` seconds."""

    def __init__(
        self,
        window: float,
        flush: Callable[[], None],
    ) -> None:
        self.window = window
        self.flush = flush
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._dirty = False

    def bump(self) -> None:
        with self._lock:
            self._dirty = True
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.window, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
        try:
            self.flush()
        except Exception:  # noqa: BLE001
            log.exception("debounced flush failed")

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


class Watcher:
    """Long-running watcher for a single repo."""

    def __init__(
        self,
        repo: Path,
        *,
        project: str | None = None,
        debounce: float = DEFAULT_DEBOUNCE,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_sync: Callable[[SyncResult], None] | None = None,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.project = project
        self.debounce_window = debounce
        self.poll_interval = poll_interval
        self.on_sync = on_sync
        self.exclude = _default_exclude(self.repo)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._debouncer = Debouncer(debounce, self._trigger_sync)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, *, blocking: bool = False) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        target = self._run_watchdog if _watchdog_available() else self._run_poll
        if blocking:
            target()
            return
        self._thread = threading.Thread(target=target, name="cm-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._debouncer.cancel()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Implementations
    # ------------------------------------------------------------------

    def _run_watchdog(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):  # noqa: ANN001 - lib type
                if event.is_directory:
                    return
                path = Path(getattr(event, "dest_path", None) or event.src_path)
                if watcher.exclude(path):
                    return
                watcher._debouncer.bump()

        observer = Observer()
        observer.schedule(_Handler(), str(self.repo), recursive=True)
        observer.start()
        log.info("watcher started (watchdog) on %s", self.repo)
        try:
            while not self._stop.wait(0.5):
                pass
        finally:
            observer.stop()
            observer.join(timeout=3)
            log.info("watcher stopped (watchdog)")

    def _run_poll(self) -> None:
        log.info(
            "watchdog not installed; falling back to mtime polling on %s "
            "(install `watchdog` for native events)",
            self.repo,
        )
        last_mtime = self._max_mtime()
        last_head = self._git_head()
        while not self._stop.wait(self.poll_interval):
            mtime = self._max_mtime()
            head = self._git_head()
            if mtime != last_mtime or head != last_head:
                last_mtime = mtime
                last_head = head
                self._debouncer.bump()

    def _max_mtime(self) -> float:
        latest = 0.0
        for root, dirs, files in _safe_walk(self.repo):
            r = Path(root)
            dirs[:] = [d for d in dirs if not self.exclude(r / d)]
            for name in files:
                p = r / name
                if self.exclude(p):
                    continue
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if mt > latest:
                    latest = mt
        return latest

    def _git_head(self) -> str | None:
        try:
            from ..orchestrator import git_delta

            return git_delta.head_sha(self.repo) if git_delta.is_git_repo(self.repo) else None
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def _trigger_sync(self) -> None:
        log.debug("watcher firing sync for %s", self.repo)
        try:
            result = sync_repo(self.repo, project=self.project, trigger="watcher")
        except Exception:  # noqa: BLE001
            log.exception("watcher sync failed")
            return
        if self.on_sync:
            try:
                self.on_sync(result)
            except Exception:  # noqa: BLE001
                log.exception("on_sync callback raised")


def _watchdog_available() -> bool:
    try:
        import watchdog  # noqa: F401
        import watchdog.observers  # noqa: F401

        return True
    except ImportError:
        return False


def _safe_walk(root: Path):
    import os

    for entry in os.walk(root):
        yield entry


def run_foreground(repo: Path, *, project: str | None = None) -> None:
    """Blocking CLI entry: start the watcher until Ctrl-C."""
    w = Watcher(repo, project=project)
    w.start(blocking=False)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        w.stop()
