"""Guards against pointing the watcher at filesystem roots that would
walk an unbounded number of files (HOME, /, /tmp, …).

A rogue watch on ``$HOME`` re-walks every checkout, IDE cache, browser
profile, and node_modules on the machine. It saturates CPU, contends with
Ollama, and produces useless indexes.
"""

from __future__ import annotations

from pathlib import Path


class UnsafeWatchRootError(ValueError):
    """Raised when the watcher is asked to watch a forbidden root."""


def _system_unsafe_roots() -> set[Path]:
    """Coarse set of filesystem roots that must never be watched.

    Resolved at call time so symlinks (``/var -> /private/var`` on macOS)
    line up with whatever the user passes in.
    """
    candidates = [
        Path("/"),
        Path.home(),
        Path("/tmp"),
        Path("/var"),
        Path("/private"),
        Path("/etc"),
        Path("/usr"),
        Path("/System"),
        Path("/Library"),
        Path("/opt"),
        Path("/Applications"),
        Path("C:/"),
        Path("C:/Users"),
        Path("C:/Windows"),
        Path("C:/Program Files"),
    ]
    out: set[Path] = set()
    for p in candidates:
        try:
            out.add(p.resolve())
        except (OSError, RuntimeError):
            continue
    return out


# Contiguous path segments that mark a directory as ephemeral / per-session:
# it exists now but is created fresh per agent session (or per plugin version)
# and discarded, so it must never receive a *persistent* OS autostart agent.
_EPHEMERAL_MARKERS: tuple[tuple[str, ...], ...] = (
    (".claude", "homunculus"),  # Claude Code per-session worktrees
    (".cursor", "worktrees"),  # Cursor per-session worktrees
    (".claude", "plugins", "cache"),  # versioned plugin cache dirs
)


def is_ephemeral_watch_dir(root: Path | str) -> bool:
    """True if ``root`` lives under a known ephemeral / per-session location.

    Such dirs (agent session worktrees, versioned plugin caches) churn
    constantly. A session-scoped in-process watcher on one is fine — it dies
    with the session — but a *persistent* OS autostart agent for each leaks
    unbounded launchd/systemd/schtasks units. Used to gate persistent
    registration only, not general watching.
    """
    parts = tuple(p.lower() for p in Path(root).expanduser().resolve().parts)
    for marker in _EPHEMERAL_MARKERS:
        span = len(marker)
        for i in range(len(parts) - span + 1):
            if parts[i : i + span] == marker:
                return True
    return False


def assert_safe_watch_root(root: Path | str) -> Path:
    """Resolve ``root`` and reject HOME / filesystem roots / system dirs.

    Returns the resolved :class:`Path` on success. Raises
    :class:`UnsafeWatchRootError` if the path is on the forbidden list
    or equals one of the user's HOME / system roots.
    """
    resolved = Path(root).expanduser().resolve()
    forbidden = _system_unsafe_roots()
    if resolved in forbidden:
        raise UnsafeWatchRootError(
            f"refusing to watch {resolved!s}: this is a filesystem / HOME / "
            "system root. Point the watcher at a specific project directory "
            "instead (e.g. ~/Workspace/my-repo)."
        )
    return resolved
