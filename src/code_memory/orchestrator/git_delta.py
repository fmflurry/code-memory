"""Git-aware delta detection for incremental ingestion.

Given a repo root and a base commit, produce three lists:
  - changed (added / modified / renamed-new) -> reingest
  - deleted (removed / renamed-old)          -> drop from index
  - dirty   (uncommitted worktree changes)   -> reingest

All paths returned are absolute, resolved.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(repo: Path, *args: str, check: bool = True, timeout: float = 30.0) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise GitError("git executable not found on PATH") from e
    except subprocess.SubprocessError as e:
        raise GitError(f"git invocation failed: {e}") from e
    if check and out.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed (exit {out.returncode}): {out.stderr.strip()}"
        )
    return out.stdout


def is_git_repo(root: str | Path) -> bool:
    try:
        out = _run(Path(root), "rev-parse", "--is-inside-work-tree", check=False)
    except GitError:
        return False
    return out.strip() == "true"


def head_sha(root: str | Path) -> str:
    return _run(Path(root), "rev-parse", "HEAD").strip()


def current_branch(root: str | Path) -> str | None:
    out = _run(Path(root), "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    return out if out and out != "HEAD" else None


def is_reachable(root: str | Path, sha: str) -> bool:
    try:
        _run(Path(root), "cat-file", "-e", f"{sha}^{{commit}}", check=True)
        return True
    except GitError:
        return False


def resolve_ref(root: str | Path, ref: str) -> str:
    """Resolve a ref (branch / tag / sha) to a full SHA. Raises if unknown."""
    return _run(Path(root), "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


@dataclass
class Delta:
    changed: list[Path] = field(default_factory=list)
    deleted: list[Path] = field(default_factory=list)
    dirty: list[Path] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.changed or self.deleted or self.dirty)

    def reingest_paths(self) -> list[Path]:
        # de-dup while preserving order
        seen: set[Path] = set()
        out: list[Path] = []
        for p in self.changed + self.dirty:
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
        return out


def diff(root: str | Path, base_sha: str, head: str = "HEAD") -> Delta:
    """Compute path-level delta between base_sha and head (committed only)."""
    repo = Path(root).resolve()
    out = _run(repo, "diff", "--name-status", "-M", base_sha, head)
    delta = Delta()
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        # M / A / D / T => 2 fields ; R### / C### => 3 fields (old, new)
        code = status[0]
        if code in ("R", "C") and len(parts) >= 3:
            old_abs = (repo / parts[1]).resolve()
            new_abs = (repo / parts[2]).resolve()
            delta.deleted.append(old_abs)
            delta.changed.append(new_abs)
        elif code == "D" and len(parts) >= 2:
            delta.deleted.append((repo / parts[1]).resolve())
        elif code in ("A", "M", "T") and len(parts) >= 2:
            delta.changed.append((repo / parts[1]).resolve())
        # anything else (U/X/B) -> skip silently
    return delta


def dirty_files(root: str | Path) -> list[Path]:
    """Return absolute paths of files with uncommitted changes (modified, added, untracked).

    Deleted-but-not-committed files are not reported here; they're handled by
    a future commit-driven delete.
    """
    repo = Path(root).resolve()
    out = _run(repo, "status", "--porcelain=v1", "--untracked-files=all")
    paths: list[Path] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        rest = line[3:]
        # rename in index: "R  old -> new"
        if "->" in rest:
            rest = rest.split("->", 1)[1].strip()
        # ignored / deleted both index+worktree -> skip
        if xy == "!!" or "D" in xy:
            continue
        path = (repo / rest).resolve()
        if path.is_file():
            paths.append(path)
    return paths


def changed_since(root: str | Path, base_sha: str, *, include_dirty: bool = True) -> Delta:
    """Convenience: delta from base_sha to HEAD, plus optional dirty worktree."""
    d = diff(root, base_sha, "HEAD")
    if include_dirty:
        d.dirty.extend(dirty_files(root))
    return d
