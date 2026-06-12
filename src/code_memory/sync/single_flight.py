"""Single-flight guard for background rebuilds.

Ensures at most one concurrent rebuild runs for a given (root, project)
pair.  Uses an in-process ``asyncio.Lock``-based registry (fast path for
the MCP server which is entirely single-process) layered on a PID-lock
file (cross-process safety for CLI paths and Phase-3 integration).

Public API
----------
``try_acquire(root, project) -> bool``
    Return ``True`` and mark the slot taken iff no rebuild is currently
    running.  Caller MUST call ``release(root, project)`` when done.

``release(root, project) -> None``
    Release the slot unconditionally.

``async_rebuild_context(root, project)``
    Async context manager — acquires on enter, releases on exit,
    raises ``AlreadyRunning`` when another rebuild holds the slot.

Both the in-process lock and the PID file are checked; either can block
acquisition.  Stale PID files (dead PID or age > TTL) are silently
removed so a crashed rebuild doesn't block forever.

Design notes
------------
* Immutable key tuple — never mutated after creation.
* Errors are always raised, never swallowed silently.
* ``try_acquire`` is safe to call from any thread or async context.
* The module is install-agnostic (stdlib only; no extra dependencies).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

__all__ = [
    "AlreadyRunning",
    "async_rebuild_context",
    "release",
    "try_acquire",
]

log = logging.getLogger("codememory.single_flight")

# Maximum age of a lockfile before it is considered stale regardless of
# whether the owner PID is still alive.  Crash-safe floor: any rebuild
# that takes longer than this is treated as hung and evicted.
_LOCK_TTL_SECONDS: float = float(
    os.environ.get("CODE_MEMORY_REBUILD_LOCK_TTL", "3600")
)

# Directory for PID lock files.  Override via env for tests.
_LOCK_DIR_ENV = "CODE_MEMORY_LOCK_DIR"


def _lock_dir() -> Path:
    override = os.environ.get(_LOCK_DIR_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "code-memory" / "locks"


# ---------------------------------------------------------------------------
# In-process registry
# ---------------------------------------------------------------------------

# Protects the two dicts below.  Plain threading.Lock (not asyncio) so
# synchronous callers (CLI) can use ``try_acquire`` without an event loop.
_registry_lock = threading.Lock()

# Keys: (root_str, project).  Values: asyncio.Lock per slot.
_async_locks: dict[tuple[str, str], asyncio.Lock] = {}

# Tracks which slots are currently held (independent of the asyncio lock
# so synchronous callers can check without entering an event loop).
_held: set[tuple[str, str]] = set()


def _key(root: Path, project: str) -> tuple[str, str]:
    return (str(root.resolve()), project)


def _get_or_create_async_lock(k: tuple[str, str]) -> asyncio.Lock:
    with _registry_lock:
        if k not in _async_locks:
            _async_locks[k] = asyncio.Lock()
        return _async_locks[k]


# ---------------------------------------------------------------------------
# PID lock file helpers
# ---------------------------------------------------------------------------


def _pid_file(k: tuple[str, str]) -> Path:
    """Return the PID-file path for key ``k``.

    The filename is derived from the key so two different projects in the
    same root directory get separate files.
    """
    # Use a safe filename: replace path separators and spaces.
    root_part = k[0].replace("/", "_").replace("\\", "_").replace(" ", "_")
    project_part = k[1].replace("/", "_").replace(" ", "_")
    # Trim to avoid hitting OS filename-length limits.
    name = f"{root_part[:64]}__{project_part[:32]}.lock"
    return _lock_dir() / name


def _pid_alive(pid: int) -> bool:
    """Return ``True`` iff *pid* is a running process (best-effort)."""
    if pid <= 0:
        return False
    try:
        # os.kill(pid, 0) raises OSError(ESRCH) when the PID is dead.
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid_file(path: Path) -> int | None:
    """Parse the PID from a lockfile.  Returns ``None`` on any error."""
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def _is_stale(path: Path) -> bool:
    """Return ``True`` iff the PID file should be evicted."""
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True  # missing → stale
    if age > _LOCK_TTL_SECONDS:
        return True
    pid = _read_pid_file(path)
    if pid is None:
        return True
    return not _pid_alive(pid)


def _acquire_pid_file(path: Path) -> bool:
    """Try to create the PID lockfile.

    Returns ``True`` on success, ``False`` if another live process holds it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Clean up stale locks before trying to acquire.
    if path.exists() and _is_stale(path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    # O_CREAT | O_EXCL gives us atomic create-or-fail semantics.
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Some other process beat us — check liveness.
        if _is_stale(path):
            # Race: it became stale between our check and the open.
            # Remove and retry once.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return False
        else:
            return False
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def _release_pid_file(path: Path) -> None:
    """Remove our PID lockfile.  Silent if already gone."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("single_flight: could not remove lock file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class AlreadyRunning(RuntimeError):
    """Raised when a rebuild slot is already occupied."""


def try_acquire(root: Path, project: str) -> bool:
    """Attempt to acquire the rebuild slot for ``(root, project)``.

    Returns ``True`` if this caller now holds the slot.
    Returns ``False`` (never raises) if another holder exists.

    Thread-safe.  Must call :func:`release` when done.
    """
    k = _key(root, project)
    with _registry_lock:
        if k in _held:
            return False
        pid_path = _pid_file(k)
        if not _acquire_pid_file(pid_path):
            return False
        _held.add(k)
    return True


def release(root: Path, project: str) -> None:
    """Release the rebuild slot for ``(root, project)``.

    Idempotent — safe to call even when the slot was never acquired.
    """
    k = _key(root, project)
    with _registry_lock:
        _held.discard(k)
        _release_pid_file(_pid_file(k))


@asynccontextmanager
async def async_rebuild_context(
    root: Path, project: str
) -> AsyncIterator[None]:
    """Async context manager that holds the rebuild slot.

    Acquires the slot on enter, releases on exit (even on exception).
    Raises :exc:`AlreadyRunning` immediately if another holder exists
    (does NOT block waiting for the lock to free).

    Combines both the in-process asyncio lock (zero-overhead for the
    common single-process MCP server path) and the PID file (cross-
    process safety for multi-process CLI paths).

    Example::

        async with async_rebuild_context(repo, project):
            await rebuild()
    """
    k = _key(root, project)
    lock = _get_or_create_async_lock(k)

    # Non-blocking in-process lock check.
    if lock.locked():
        raise AlreadyRunning(
            f"Rebuild already running for project={project!r} root={root}"
        )

    # PID file check (cross-process).
    pid_path = _pid_file(k)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    if not _acquire_pid_file(pid_path):
        raise AlreadyRunning(
            f"Rebuild already running (pid file) for project={project!r} root={root}"
        )

    # Grab the in-process asyncio lock (non-blocking — we checked above).
    try:
        await asyncio.wait_for(lock.acquire(), timeout=0.0)
    except asyncio.TimeoutError:
        _release_pid_file(pid_path)
        raise AlreadyRunning(
            f"Rebuild already running (async lock) for project={project!r} root={root}"
        )

    with _registry_lock:
        _held.add(k)

    try:
        yield
    finally:
        with _registry_lock:
            _held.discard(k)
        lock.release()
        _release_pid_file(pid_path)
