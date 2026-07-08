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


EXCLUDED_ROOT_DIRS: tuple[str, ...] = (
    # VCS / project state
    ".git",
    "data",
    # Virtualenvs / package roots
    ".venv",
    "node_modules",
    # Build outputs
    "dist",
    "out-tsc",
    "build",
    "target",
    "coverage",
    # Framework / bundler caches
    ".angular",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".parcel-cache",
    ".cache",
    # Python caches
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    # Editor metadata
    ".idea",
    ".vscode",
    # Agentic tool caches (high write churn, no source value)
    ".opencode",
    ".serena",
    ".claude",
    ".cursor",
    ".windsurf",
    ".clavix",
)


def _default_exclude(repo: Path) -> ExcludeFn:
    repo_root = repo.resolve()
    blocked = tuple((repo_root / name).resolve() for name in EXCLUDED_ROOT_DIRS)

    def exclude(p: Path) -> bool:
        try:
            r = p.resolve()
        except OSError:
            return True
        for b in blocked:
            try:
                r.relative_to(b)
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
                watcher._handle_path(path)

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
    # Event routing
    # ------------------------------------------------------------------

    def _is_ref_event(self, path: Path) -> bool:
        """True when ``path`` is a git ref whose change should re-sync."""
        try:
            rel = path.resolve().relative_to(self.repo)
        except (OSError, ValueError):
            return False
        parts = rel.parts
        if not parts or parts[0] != ".git":
            return False
        if parts == (".git", "HEAD"):
            return True
        if len(parts) >= 4 and parts[1:3] == ("refs", "heads"):
            return True
        return False

    def _handle_path(self, path: Path) -> None:
        """Decide whether ``path`` should trigger a debounced sync."""
        if self._is_ref_event(path):
            self._debouncer.bump()
            return
        if self.exclude(path):
            return
        self._debouncer.bump()

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


# ---------------------------------------------------------------------------
# Phase 2: single-process, many-roots watcher daemon.
#
# ``Watcher``/``run_foreground`` above remain the single-repo entry point
# (still used directly by ``code-memory watch``). Everything below adds a
# second, independent entry point: one process, one ``watchdog`` Observer,
# many watched roots — driven by the on-disk watch registry so a single
# long-running daemon replaces one-launchd-unit-per-repo.
# ---------------------------------------------------------------------------

import json  # noqa: E402
import os  # noqa: E402
import signal  # noqa: E402
from collections.abc import Iterable  # noqa: E402
from functools import lru_cache  # noqa: E402
from typing import Any  # noqa: E402

from ..config import watch_registry_path, watchd_state_path  # noqa: E402
from . import registry  # noqa: E402
from .safety import (  # noqa: E402
    UnsafeWatchRootError,
    assert_safe_watch_root,
    is_non_persistent_watch_dir,
)

try:
    from watchdog.events import FileSystemEventHandler
except ImportError:  # pragma: no cover - watchdog optional at runtime

    class FileSystemEventHandler:  # type: ignore[no-redef]
        """Fallback stub used only when ``watchdog`` isn't installed."""


def _is_ref_event_for_root(path: Path, repo: Path) -> bool:
    """Free-function twin of ``Watcher._is_ref_event`` for a given *repo* root.

    Duplicated rather than shared so the single-repo ``Watcher`` stays
    untouched; both implementations must agree on what counts as a git-ref
    change worth an immediate re-sync (``.git/HEAD`` or ``.git/refs/heads/*``).
    """
    try:
        rel = path.resolve().relative_to(repo)
    except (OSError, ValueError):
        return False
    parts = rel.parts
    if not parts or parts[0] != ".git":
        return False
    if parts == (".git", "HEAD"):
        return True
    if len(parts) >= 4 and parts[1:3] == ("refs", "heads"):
        return True
    return False


@lru_cache(maxsize=256)
def _cached_default_exclude(resolved: Path) -> ExcludeFn:
    """Memoized :func:`_default_exclude` per resolved root.

    ``_default_exclude`` builds a tuple of ~25 resolved exclusion Paths on
    every call. Repeated add/remove churn against the *same* root (registry
    reconcile loops, add/remove-cycle tests) would otherwise mint a fresh
    tuple every cycle that only gets freed once every handler referencing
    it is dropped — bounded here to at most 256 distinct roots.
    """
    return _default_exclude(resolved)


class _RootHandler(FileSystemEventHandler):  # type: ignore[misc]
    """Per-root watchdog event handler feeding a per-root ``Debouncer``."""

    def __init__(self, root: Path, exclude: ExcludeFn, debouncer: Debouncer) -> None:
        self.root = root
        self.exclude = exclude
        self.debouncer = debouncer

    def on_any_event(self, event: Any) -> None:  # noqa: ANN401 - lib type
        if event.is_directory:
            return
        path = Path(getattr(event, "dest_path", None) or event.src_path)
        self._handle_path(path)

    def _handle_path(self, path: Path) -> None:
        if _is_ref_event_for_root(path, self.root):
            self.debouncer.bump()
            return
        if self.exclude(path):
            return
        self.debouncer.bump()


class _RegistryHandler(FileSystemEventHandler):  # type: ignore[misc]
    """Watches the registry file's parent dir, filtered to its own filename."""

    def __init__(self, filename: str, debouncer: Debouncer) -> None:
        self.filename = filename
        self.debouncer = debouncer

    def on_any_event(self, event: Any) -> None:  # noqa: ANN401 - lib type
        if event.is_directory:
            return
        path = Path(getattr(event, "dest_path", None) or event.src_path)
        if path.name == self.filename:
            self.debouncer.bump()


class MultiRootWatcher:
    """One ``watchdog`` Observer fanning out to many independently-debounced
    watched roots, reconciled against the on-disk watch registry."""

    def __init__(
        self,
        *,
        observer: Any = None,
        debounce: float = DEFAULT_DEBOUNCE,
    ) -> None:
        if observer is not None:
            self.observer = observer
        else:
            from watchdog.observers import Observer

            self.observer = Observer()
        self.debounce = debounce
        self._lock = threading.Lock()
        self._watches: dict[Path, Any] = {}
        self._debouncers: dict[Path, Debouncer] = {}
        self._handlers: dict[Path, _RootHandler] = {}
        self._slugs: dict[Path, str] = {}
        self._registry_debouncer: Debouncer | None = None
        self._registry_watch: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self.observer.start()

    def stop(self) -> None:
        with self._lock:
            debouncers = list(self._debouncers.values())
            registry_debouncer = self._registry_debouncer
        for debouncer in debouncers:
            debouncer.cancel()
        if registry_debouncer is not None:
            registry_debouncer.cancel()
        self.observer.stop()
        self.observer.join(timeout=5)

    # ------------------------------------------------------------------
    # Per-root watch management
    # ------------------------------------------------------------------

    def add_root(self, root: Path | str, slug: str) -> None:
        """Idempotent: re-adding an already-watched root is a no-op."""
        resolved = Path(root).resolve()
        with self._lock:
            if resolved in self._watches:
                return
            exclude = _cached_default_exclude(resolved)
            debouncer = Debouncer(self.debounce, lambda: self._trigger_sync(resolved, slug))
            handler = _RootHandler(resolved, exclude, debouncer)
            watch = self.observer.schedule(handler, str(resolved), recursive=True)
            self._watches[resolved] = watch
            self._debouncers[resolved] = debouncer
            self._handlers[resolved] = handler
            self._slugs[resolved] = slug

    def remove_root(self, root: Path | str) -> None:
        """No-op when *root* isn't currently watched."""
        resolved = Path(root).resolve()
        with self._lock:
            if resolved not in self._watches:
                return
            debouncer = self._debouncers[resolved]
            watch = self._watches[resolved]
            debouncer.cancel()
            self.observer.unschedule(watch)
            del self._watches[resolved]
            del self._debouncers[resolved]
            del self._handlers[resolved]
            del self._slugs[resolved]

    def watched_roots(self) -> list[Path]:
        """Thread-safe snapshot of currently-watched roots.

        Returns a plain ``list`` copy — never a live view — so callers
        (e.g. ``_safe_reconcile``'s state-file snapshot) never race a
        concurrent ``add_root``/``remove_root`` mutating ``_watches``.
        """
        with self._lock:
            return list(self._watches.keys())

    def reconcile(self, desired: dict[Path, str]) -> None:
        """Delta-only sync against *desired* (root -> slug).

        Roots that fail :func:`assert_safe_watch_root` or for which
        :func:`is_non_persistent_watch_dir` is true are skipped entirely.
        Unchanged (same-path, same-slug) roots are never touched — no
        cancel/re-schedule churn. A root whose path is unchanged but whose
        desired slug differs from the currently-bound slug is rebound
        (removed then re-added) so its handler/debouncer closure captures
        the new slug instead of silently keeping the stale one.
        """
        filtered: dict[Path, str] = {}
        for root, slug in desired.items():
            try:
                resolved = assert_safe_watch_root(root)
            except UnsafeWatchRootError:
                log.warning("reconcile: skipping unsafe watch root %s", root)
                continue
            if is_non_persistent_watch_dir(resolved):
                continue
            filtered[resolved] = slug

        with self._lock:
            current_slugs = dict(self._slugs)

        current_keys = set(current_slugs.keys())
        desired_keys = set(filtered.keys())
        to_remove = current_keys - desired_keys
        to_add = desired_keys - current_keys
        to_rebind = {
            root
            for root in current_keys & desired_keys
            if current_slugs[root] != filtered[root]
        }

        for stale_root in to_remove:
            self.remove_root(stale_root)
        for changed_root in to_rebind:
            self.remove_root(changed_root)
            self.add_root(changed_root, filtered[changed_root])
        for new_root in to_add:
            self.add_root(new_root, filtered[new_root])

    # ------------------------------------------------------------------
    # Registry self-watch
    # ------------------------------------------------------------------

    def watch_registry(
        self,
        registry_path: Path,
        *,
        debounce: float,
        on_change: Callable[[], None],
    ) -> None:
        """Self-watch the registry file's parent dir (non-recursive).

        Re-entrant: cancels the previous registry debouncer and unschedules
        the previous registry watch (if any) before installing the new
        pair, so repeated calls (e.g. daemon restart-in-place, tests) never
        leak the prior debouncer/watch.
        """
        registry_path = Path(registry_path).resolve()
        parent = registry_path.parent
        parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            old_debouncer = self._registry_debouncer
            old_watch = self._registry_watch

        if old_debouncer is not None:
            old_debouncer.cancel()
        if old_watch is not None:
            self.observer.unschedule(old_watch)

        debouncer = Debouncer(debounce, on_change)
        handler = _RegistryHandler(registry_path.name, debouncer)
        watch = self.observer.schedule(handler, str(parent), recursive=False)
        with self._lock:
            self._registry_debouncer = debouncer
            self._registry_watch = watch

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def _trigger_sync(self, root: Path, slug: str) -> None:
        log.debug("multiroot watcher firing sync for %s", root)
        try:
            sync_repo(root, project=slug, trigger="watchd")
        except Exception:  # noqa: BLE001
            log.exception("multiroot watcher sync failed for %s", root)


def write_daemon_state(watched_roots: Iterable[Path | str]) -> None:
    """Persist ``{pid, watched_roots, ts}`` to :func:`watchd_state_path`.

    Written atomically (temp file + ``os.replace``) so a concurrent reader
    never observes a half-written state file.
    """
    state_path = watchd_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "watched_roots": sorted(str(Path(r).resolve()) for r in watched_roots),
        "ts": time.time(),
    }
    tmp_path = state_path.with_name(f".{state_path.name}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, state_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _safe_reconcile(
    watcher: MultiRootWatcher,
    on_reconcile: Callable[[MultiRootWatcher], None] | None = None,
) -> None:
    """Guarded shared reconcile entrypoint for the SIGHUP and registry-watch
    triggers.

    A reconcile failure (bad registry data, a watcher-internal exception,
    a state-write failure) must never propagate and kill the daemon — both
    call sites route through this single guarded helper instead of each
    growing their own try/except.
    """
    try:
        desired = {Path(root): entry.slug for root, entry in registry.load().items()}
        watcher.reconcile(desired)
        write_daemon_state(watcher.watched_roots())
        if on_reconcile is not None:
            on_reconcile(watcher)
    except Exception:  # noqa: BLE001
        log.exception("reconcile failed")


def run_daemon(
    *,
    observer: Any = None,
    stop_event: threading.Event | None = None,
    reconcile_debounce: float = 1.5,
    poll_interval: float = 0.5,
    on_reconcile: Callable[[MultiRootWatcher], None] | None = None,
) -> None:
    """Blocking single-process daemon watching every registry-listed root.

    Does an initial ``reconcile(registry.load())``, self-watches the
    registry file so newly-registered/removed roots are picked up without
    a restart, and writes daemon state after every reconcile. Returns once
    *stop_event* is set.
    """
    stop_event = stop_event if stop_event is not None else threading.Event()
    watcher = MultiRootWatcher(observer=observer)
    watcher.start()

    try:
        _safe_reconcile(watcher, on_reconcile)

        watcher.watch_registry(
            watch_registry_path(),
            debounce=reconcile_debounce,
            on_change=lambda: _safe_reconcile(watcher, on_reconcile),
        )

        try:
            signal.signal(signal.SIGHUP, lambda *_args: _safe_reconcile(watcher, on_reconcile))
        except (ValueError, AttributeError):
            # ValueError: not running in the main thread of the main
            # interpreter (e.g. under test, or embedded). AttributeError:
            # platform has no SIGHUP (Windows). Both are fine to ignore —
            # the registry self-watch already covers the same trigger.
            pass

        while not stop_event.wait(poll_interval):
            pass
    finally:
        watcher.stop()
