"""Snapshot storage backend: orphan git branch ``codemem-snapshots``.

Layout on the branch::

    snapshots/<sha>.cmsnap   # one tar.gz blob per ingested commit
    manifests/<sha>.json     # mirror of the snapshot manifest (cheap lookup)
    index.json               # { sha: {created_at, size, parent_sha?, ...} }

The branch has no shared history with ``main``; it is pure storage. Any
contributor can publish; content-addressing by SHA makes concurrent
pushes for the same commit converge (identical blob = no-op).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_BRANCH = "codemem-snapshots"


class StoreError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True, timeout: float = 60.0) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and out.returncode != 0:
        raise StoreError(
            f"git {' '.join(args)} failed (exit {out.returncode}): {out.stderr.strip()}"
        )
    return out.stdout


@dataclass(frozen=True)
class StoreEntry:
    sha: str
    size: int
    created_at: float


class SnapshotStore:
    """Git-backed snapshot storage (no external infra).

    Operations:
      - ``fetch()``             — fetch the snapshot branch from origin
      - ``has(sha)``            — check local existence
      - ``read(sha) -> bytes``  — extract blob bytes
      - ``write(sha, data)``    — write blob, commit, push (best-effort)
      - ``list_local() / list_remote()``
      - ``gc(keep_last)``       — prune old snapshots locally + remote
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        branch: str = DEFAULT_BRANCH,
        remote: str = "origin",
    ) -> None:
        self.repo = Path(repo_root).resolve()
        self.branch = branch
        self.remote = remote
        if not (self.repo / ".git").exists():
            raise StoreError(f"not a git repo: {self.repo}")

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def fetch(self) -> bool:
        """Fetch the snapshot branch from remote. Returns False if no remote."""
        if not self._has_remote():
            return False
        try:
            _git(
                self.repo,
                "fetch",
                self.remote,
                f"+refs/heads/{self.branch}:refs/remotes/{self.remote}/{self.branch}",
                check=True,
            )
            return True
        except StoreError:
            # remote may not have the branch yet — that's not an error
            return False

    def has(self, sha: str) -> bool:
        return self._blob_oid(sha) is not None

    def read(self, sha: str) -> bytes:
        oid = self._blob_oid(sha)
        if oid is None:
            raise StoreError(f"snapshot {sha} not found in {self.branch}")
        out = subprocess.run(
            ["git", "-C", str(self.repo), "cat-file", "blob", oid],
            capture_output=True,
            check=True,
        )
        return out.stdout

    def list_local(self) -> list[StoreEntry]:
        return self._list_at(self._local_ref())

    def list_remote(self) -> list[StoreEntry]:
        return self._list_at(self._remote_ref())

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    def write(
        self,
        sha: str,
        blob: bytes,
        *,
        manifest: dict[str, object] | None = None,
        message: str | None = None,
        push: bool = True,
    ) -> bool:
        """Add ``blob`` for ``sha`` to the snapshot branch.

        If the SHA already exists with identical content, this is a no-op
        (returns False). Otherwise it commits and (optionally) pushes.
        Returns True iff a new commit was created.
        """
        if self.has(sha):
            existing = self.read(sha)
            if existing == blob:
                return False
        new_blob_oid = self._hash_object(blob)
        manifest_oid: str | None = None
        if manifest is not None:
            manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode()
            manifest_oid = self._hash_object(manifest_bytes)
        parent_commit = self._local_commit() or self._remote_commit()
        index_entries = self._read_index(parent_commit) if parent_commit else {}
        index_entries[sha] = {
            "size": len(blob),
            "created_at": _now(),
        }
        index_oid = self._hash_object(
            json.dumps(index_entries, sort_keys=True, indent=2).encode()
        )

        # Build a tree with all existing entries + new blob/manifest
        tree_entries = self._tree_entries(parent_commit) if parent_commit else {}
        tree_entries[f"snapshots/{sha}.cmsnap"] = ("100644", "blob", new_blob_oid)
        if manifest_oid:
            tree_entries[f"manifests/{sha}.json"] = ("100644", "blob", manifest_oid)
        tree_entries["index.json"] = ("100644", "blob", index_oid)

        tree_oid = self._mktree(tree_entries)

        commit_msg = message or f"codememory: add snapshot {sha[:12]}"
        if parent_commit:
            commit_oid = _git(
                self.repo, "commit-tree", tree_oid, "-p", parent_commit, "-m", commit_msg
            ).strip()
        else:
            commit_oid = _git(self.repo, "commit-tree", tree_oid, "-m", commit_msg).strip()

        _git(self.repo, "update-ref", f"refs/heads/{self.branch}", commit_oid)
        if push and self._has_remote():
            try:
                _git(
                    self.repo,
                    "push",
                    self.remote,
                    f"refs/heads/{self.branch}:refs/heads/{self.branch}",
                    check=True,
                )
            except StoreError:
                # remote moved; try once with --force-with-lease after refetch
                self.fetch()
                _git(
                    self.repo,
                    "push",
                    self.remote,
                    f"refs/heads/{self.branch}:refs/heads/{self.branch}",
                    "--force-with-lease",
                    check=False,
                )
        return True

    def gc(self, keep_last: int, *, push: bool = True) -> int:
        """Drop all but the ``keep_last`` most recent snapshots. Returns count removed."""
        entries = sorted(self.list_local(), key=lambda e: e.created_at, reverse=True)
        if len(entries) <= keep_last:
            return 0
        keep = {e.sha for e in entries[:keep_last]}
        parent_commit = self._local_commit()
        if parent_commit is None:
            return 0
        tree_entries = self._tree_entries(parent_commit)
        removed = 0
        for path in list(tree_entries):
            if not (path.startswith("snapshots/") or path.startswith("manifests/")):
                continue
            sha = Path(path).stem
            if sha not in keep:
                del tree_entries[path]
                removed += 1
        if removed == 0:
            return 0
        index_entries = self._read_index(parent_commit)
        index_entries = {k: v for k, v in index_entries.items() if k in keep}
        tree_entries["index.json"] = (
            "100644",
            "blob",
            self._hash_object(
                json.dumps(index_entries, sort_keys=True, indent=2).encode()
            ),
        )
        tree_oid = self._mktree(tree_entries)
        commit_oid = _git(
            self.repo,
            "commit-tree",
            tree_oid,
            "-p",
            parent_commit,
            "-m",
            f"codememory: gc keep_last={keep_last}",
        ).strip()
        _git(self.repo, "update-ref", f"refs/heads/{self.branch}", commit_oid)
        if push and self._has_remote():
            try:
                _git(
                    self.repo,
                    "push",
                    self.remote,
                    f"refs/heads/{self.branch}:refs/heads/{self.branch}",
                    "--force-with-lease",
                )
            except StoreError:
                pass
        return removed

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _has_remote(self) -> bool:
        out = _git(self.repo, "remote", check=False).strip().splitlines()
        return self.remote in out

    def _local_ref(self) -> str | None:
        out = _git(
            self.repo, "rev-parse", "--verify", f"refs/heads/{self.branch}", check=False
        ).strip()
        return out or None

    def _remote_ref(self) -> str | None:
        out = _git(
            self.repo,
            "rev-parse",
            "--verify",
            f"refs/remotes/{self.remote}/{self.branch}",
            check=False,
        ).strip()
        return out or None

    def _local_commit(self) -> str | None:
        return self._local_ref()

    def _remote_commit(self) -> str | None:
        return self._remote_ref()

    def _blob_oid(self, sha: str) -> str | None:
        for ref_fn in (self._local_ref, self._remote_ref):
            ref = ref_fn()
            if ref is None:
                continue
            oid = self._lookup(ref, f"snapshots/{sha}.cmsnap")
            if oid:
                return oid
        return None

    def _lookup(self, ref: str, path: str) -> str | None:
        out = _git(self.repo, "ls-tree", ref, path, check=False).strip()
        if not out:
            return None
        parts = out.split()
        if len(parts) < 3:
            return None
        return parts[2]

    def _tree_entries(self, commit: str) -> dict[str, tuple[str, str, str]]:
        out = _git(self.repo, "ls-tree", "-r", commit, check=False).strip()
        entries: dict[str, tuple[str, str, str]] = {}
        for line in out.splitlines():
            if not line:
                continue
            meta, name = line.split("\t", 1)
            mode, otype, oid = meta.split()
            entries[name] = (mode, otype, oid)
        return entries

    def _read_index(self, commit: str) -> dict[str, dict[str, object]]:
        oid = self._lookup(commit, "index.json")
        if not oid:
            return {}
        out = subprocess.run(
            ["git", "-C", str(self.repo), "cat-file", "blob", oid],
            capture_output=True,
            check=True,
        )
        try:
            data = json.loads(out.stdout.decode() or "{}")
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return data  # type: ignore[return-value]

    def _list_at(self, ref: str | None) -> list[StoreEntry]:
        if ref is None:
            return []
        index = self._read_index(ref)
        entries: list[StoreEntry] = []
        for sha, meta in index.items():
            if not isinstance(meta, dict):
                entries.append(StoreEntry(sha=sha, size=0, created_at=0.0))
                continue
            raw_size = meta.get("size", 0)
            raw_ts = meta.get("created_at", 0.0)
            size = int(raw_size) if isinstance(raw_size, (int, float, str)) else 0
            ts = float(raw_ts) if isinstance(raw_ts, (int, float, str)) else 0.0
            entries.append(StoreEntry(sha=sha, size=size, created_at=ts))
        return entries

    def _hash_object(self, blob: bytes) -> str:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(blob)
            tmp_path = tmp.name
        try:
            out = _git(self.repo, "hash-object", "-w", tmp_path).strip()
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return out

    def _mktree(self, entries: dict[str, tuple[str, str, str]]) -> str:
        """Build a (possibly nested) tree from flat path -> (mode, type, oid)."""
        return _build_tree(self.repo, entries)


def _build_tree(repo: Path, entries: dict[str, tuple[str, str, str]]) -> str:
    """Recursively materialise a tree object from flat entries."""
    grouped: dict[str, dict[str, tuple[str, str, str]]] = {"": {}}
    for path, meta in entries.items():
        parts = path.split("/")
        if len(parts) == 1:
            grouped[""][parts[0]] = meta
        else:
            sub = parts[0]
            rest = "/".join(parts[1:])
            grouped.setdefault(sub, {})[rest] = meta

    # Build subtrees recursively
    leaf_lines: list[str] = []
    for name, meta in grouped[""].items():
        mode, otype, oid = meta
        leaf_lines.append(f"{mode} {otype} {oid}\t{name}")

    for sub, sub_entries in grouped.items():
        if sub == "":
            continue
        sub_oid = _build_tree(repo, sub_entries)
        leaf_lines.append(f"040000 tree {sub_oid}\t{sub}")

    payload = "\n".join(leaf_lines) + "\n"
    out = subprocess.run(
        ["git", "-C", str(repo), "mktree"],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _now() -> float:
    import time

    return time.time()


# silence unused import warning
_ = Iterable
