"""Guards against pointing the watcher or ingest command at filesystem
roots that would walk an unbounded number of files (HOME, /, /tmp, …).

A rogue watch (or ingest) on ``$HOME`` re-walks every checkout, IDE cache,
browser profile, and node_modules on the machine.  It saturates CPU,
contends with Ollama, and produces useless indexes.
"""

from __future__ import annotations

from pathlib import Path


class UnsafeWatchRootError(ValueError):
    """Raised when the watcher is asked to watch a forbidden root."""


class UnsafeIngestRootError(ValueError):
    """Raised when the ingest command is given a forbidden root.

    Distinct from :class:`UnsafeWatchRootError` so callers can handle
    the two refusal paths independently if needed.
    """


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


def is_non_persistent_watch_dir(root: Path | str) -> bool:
    """Skip-persistent-autostart gate: ephemeral dir OR linked git worktree.

    A linked worktree shares the main repo's .git; the main repo gets the
    persistent watcher, so registering a per-worktree unit only leaks units.
    """
    from ..config import is_linked_git_worktree

    return is_ephemeral_watch_dir(root) or is_linked_git_worktree(Path(root))


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


def assert_safe_ingest_root(root: Path | str) -> Path:
    """Resolve ``root`` and refuse HOME / filesystem roots / non-git dirs.

    Returns the resolved :class:`Path` when the root is safe to ingest.
    Raises :class:`UnsafeIngestRootError` in three situations:

    1. The root is one of the known forbidden system/home paths (same
       set checked by :func:`assert_safe_watch_root`).
    2. The root is the user's ``$HOME`` directory (belt-and-suspenders
       in case HOME is not in the platform unsafe-roots set).
    3. The root is not inside a git worktree (prevents minting arbitrary
       per-directory Qdrant collections for non-project dirs such as
       ``~/.claude`` or ``C:\\Users\\alexa``).

    This function is version-independent: it works whether code-memory is
    installed from PyPI, via ``uv tool``, or from an editable git clone.
    All checks run purely in Python — no subprocess required for the
    HOME/system-root tests (the git-worktree test runs ``git rev-parse``
    which is the same call already used by :func:`detect_project_slug`).

    Bypass: if the env var ``CODE_MEMORY_UNSAFE_INGEST=1`` is set, all
    checks are skipped and the resolved path is returned unconditionally.
    This escape hatch is intentionally **not** a CLI flag so it cannot be
    triggered by accident in a hook invocation.
    """
    import os as _os

    from ..config import is_inside_git_worktree

    resolved = Path(root).expanduser().resolve()

    if _os.environ.get("CODE_MEMORY_UNSAFE_INGEST", "").strip() in {"1", "true", "yes"}:
        return resolved

    # 1. Platform forbidden roots (includes HOME on most platforms).
    forbidden = _system_unsafe_roots()
    if resolved in forbidden:
        raise UnsafeIngestRootError(
            f"refusing to ingest {resolved!s}: this is a filesystem / HOME / "
            "system root. Run `code-memory ingest` from a specific git "
            "repository instead (e.g. ~/Workspace/my-repo). "
            "Set CODE_MEMORY_UNSAFE_INGEST=1 to bypass (not recommended)."
        )

    # 2. Belt-and-suspenders HOME check (covers edge cases where HOME
    #    is not canonical / resolve() differs across drives on Windows).
    try:
        home = Path.home().resolve()
    except RuntimeError:
        home = None
    if home is not None and resolved == home:
        raise UnsafeIngestRootError(
            f"refusing to ingest {resolved!s}: this is the user's home "
            "directory. Point the command at a specific project directory. "
            "Set CODE_MEMORY_UNSAFE_INGEST=1 to bypass (not recommended)."
        )

    # 3. Non-git worktree guard: refuse to mint a per-directory slug for
    #    paths that have no git ancestry (e.g. ~/.claude/scripts, C:\Users\alexa).
    if not is_inside_git_worktree(resolved):
        raise UnsafeIngestRootError(
            f"refusing to ingest {resolved!s}: no git repository found in the "
            "path ancestry. `code-memory ingest` is designed for git-tracked "
            "projects. Run `git init` first, or set CODE_MEMORY_UNSAFE_INGEST=1 "
            "to bypass (not recommended)."
        )

    return resolved
