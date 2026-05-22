"""Smart sync: snapshot pull + incremental ingest in one command.

Decision tree::

    1. HEAD unchanged AND local state matches AND no dirty files
       -> noop

    2. Local state empty
       a. snapshot for HEAD exists in store     -> pull + apply
       b. snapshot for nearest ancestor exists  -> pull + apply + incremental
       c. nothing in store                      -> full local ingest
       (then optionally publish if on canonical branch)

    3. Local state present
       a. HEAD == state.sha                     -> dirty-files incremental only
       b. HEAD newer, snapshot for HEAD exists  -> pull + apply
       c. HEAD newer, otherwise                 -> incremental from state.sha

The goal: O(seconds) on every event, never block, always converge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

from ..config import CONFIG, detect_project_slug
from ..orchestrator import git_delta
from ..orchestrator.ingest_state import IngestStateStore
from ..orchestrator.pipeline import IngestStats, Pipeline
from .snapshot import (
    Snapshot,
    apply_snapshot,
    build_snapshot,
    verify_snapshot,
)
from .store import SnapshotStore

log = logging.getLogger("codememory.sync")

Action = Literal[
    "noop",
    "pull_snapshot",
    "pull_then_incremental",
    "incremental",
    "full_ingest",
    "dirty_only",
]


@dataclass
class SyncResult:
    action: Action
    head_sha: str | None
    base_sha: str | None = None
    snapshot_sha: str | None = None
    publish: bool = False
    files_changed: int = 0
    files_deleted: int = 0
    notes: list[str] = field(default_factory=list)


def sync_repo(
    root: str | Path,
    *,
    project: str | None = None,
    publish: bool = False,
    canonical_branch: str = "main",
    trigger: str = "manual",
    fetch: bool = True,
) -> SyncResult:
    """Reconcile local code-memory state with git HEAD.

    Parameters
    ----------
    root :
        Repo root.
    project :
        Project slug (auto-detected from git toplevel if None).
    publish :
        After sync, if on ``canonical_branch`` and a fresh snapshot was
        produced locally, push it to the snapshot store.
    canonical_branch :
        Branch whose tip is considered canonical (default ``main``).
    trigger :
        Free-form tag for logging (e.g. ``post-merge``, ``watcher``).
    fetch :
        If True, ``git fetch`` the snapshot branch before lookup.
    """
    root_path = Path(root).resolve()
    slug = project or detect_project_slug(root_path)
    log.info("sync start trigger=%s project=%s root=%s", trigger, slug, root_path)

    if not git_delta.is_git_repo(root_path):
        # Not a git repo: best we can do is a full ingest
        pipe = Pipeline(project=slug)
        stats = pipe.ingest_repo(root_path, mode="full")
        return SyncResult(
            action="full_ingest",
            head_sha=None,
            files_changed=stats.files,
            notes=["not a git repository; performed full ingest"],
        )

    head = git_delta.head_sha(root_path)
    branch = git_delta.current_branch(root_path)
    store = SnapshotStore(root_path)
    if fetch:
        store.fetch()

    cfg = CONFIG.for_project(slug)
    state_store = IngestStateStore(cfg.episodic_db)
    prior = state_store.get(root_path)
    dirty = git_delta.dirty_files(root_path)

    # ---- Case 1: HEAD matches local state ---------------------------------
    if prior is not None and prior.last_sha == head:
        if not dirty:
            log.info("sync noop (head=%s, clean)", head[:12])
            return SyncResult(action="noop", head_sha=head)
        # dirty files: incremental only
        return _run_dirty_only(root_path, slug, head, dirty)

    # ---- Case 2: no local state -------------------------------------------
    if prior is None:
        if store.has(head):
            return _pull_and_apply(
                root_path, slug, head, branch, store, publish=False
            )
        ancestor = _find_ancestor_snapshot(root_path, store, head)
        if ancestor:
            return _pull_and_apply_then_incremental(
                root_path, slug, head, branch, store, ancestor, dirty
            )
        # No snapshot, no state — full ingest
        return _run_full_ingest(
            root_path,
            slug,
            head,
            branch,
            store,
            publish=publish and branch == canonical_branch,
        )

    # ---- Case 3: HEAD moved, local state stale ----------------------------
    if store.has(head):
        return _pull_and_apply(root_path, slug, head, branch, store, publish=False)

    if not git_delta.is_reachable(root_path, prior.last_sha):
        # base rewritten — fall back to ancestor snapshot or full
        ancestor = _find_ancestor_snapshot(root_path, store, head)
        if ancestor:
            return _pull_and_apply_then_incremental(
                root_path, slug, head, branch, store, ancestor, dirty
            )
        return _run_full_ingest(
            root_path,
            slug,
            head,
            branch,
            store,
            publish=publish and branch == canonical_branch,
        )

    return _run_incremental(
        root_path,
        slug,
        head,
        branch,
        base=prior.last_sha,
        store=store,
        publish=publish and branch == canonical_branch,
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def _run_dirty_only(
    root: Path, slug: str, head: str, dirty: Iterable[Path]
) -> SyncResult:
    pipe = Pipeline(project=slug)
    changed = 0
    for path in dirty:
        if not path.is_file():
            continue
        ex = pipe.reingest_file(path)
        if ex is not None:
            changed += 1
    return SyncResult(
        action="dirty_only",
        head_sha=head,
        base_sha=head,
        files_changed=changed,
        notes=["worktree dirty; re-indexed locally without changing state"],
    )


def _run_incremental(
    root: Path,
    slug: str,
    head: str,
    branch: str | None,
    *,
    base: str,
    store: SnapshotStore,
    publish: bool,
) -> SyncResult:
    pipe = Pipeline(project=slug)
    stats = pipe.ingest_repo(root, mode="incremental", since=base)
    result = SyncResult(
        action="incremental",
        head_sha=head,
        base_sha=base,
        files_changed=stats.files,
        files_deleted=stats.deleted,
    )
    if publish:
        _publish(store, slug, head, branch, result)
    return result


def _run_full_ingest(
    root: Path,
    slug: str,
    head: str,
    branch: str | None,
    store: SnapshotStore,
    *,
    publish: bool,
) -> SyncResult:
    pipe = Pipeline(project=slug)
    stats: IngestStats = pipe.ingest_repo(root, mode="full")
    result = SyncResult(
        action="full_ingest",
        head_sha=head,
        files_changed=stats.files,
    )
    if publish:
        _publish(store, slug, head, branch, result)
    return result


def _pull_and_apply(
    root: Path,
    slug: str,
    head: str,
    branch: str | None,
    store: SnapshotStore,
    *,
    publish: bool,
) -> SyncResult:
    snap_bytes = store.read(head)
    snap = _load_and_verify(snap_bytes, slug)
    apply_snapshot(snap)
    # Mirror the snapshot's state into the local ingest_state so a
    # subsequent incremental can diff from it.
    pipe = Pipeline(project=slug)
    pipe.state.set(root, sha=head, branch=branch)
    return SyncResult(
        action="pull_snapshot",
        head_sha=head,
        snapshot_sha=head,
        files_changed=snap.manifest.counts.get("vectors", 0),
    )


def _pull_and_apply_then_incremental(
    root: Path,
    slug: str,
    head: str,
    branch: str | None,
    store: SnapshotStore,
    ancestor: str,
    dirty: Iterable[Path],
) -> SyncResult:
    snap_bytes = store.read(ancestor)
    snap = _load_and_verify(snap_bytes, slug)
    apply_snapshot(snap)
    pipe = Pipeline(project=slug)
    pipe.state.set(root, sha=ancestor, branch=branch)
    stats = pipe.ingest_repo(root, mode="incremental", since=ancestor)
    return SyncResult(
        action="pull_then_incremental",
        head_sha=head,
        base_sha=ancestor,
        snapshot_sha=ancestor,
        files_changed=stats.files,
        files_deleted=stats.deleted,
    )


def _publish(
    store: SnapshotStore,
    slug: str,
    head: str,
    branch: str | None,
    result: SyncResult,
) -> None:
    """Build and push a fresh snapshot for HEAD."""
    if store.has(head):
        result.notes.append("snapshot already published")
        return
    snap = build_snapshot(
        project=slug,
        head_sha=head,
        branch=branch,
        state={"last_sha": head, "branch": branch},
    )
    import tempfile

    with tempfile.NamedTemporaryFile(
        suffix=".cmsnap", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        snap.write(tmp_path)
        data = tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)
    manifest_dict: dict[str, object] = {
        "format_version": snap.manifest.format_version,
        "project": snap.manifest.project,
        "head_sha": snap.manifest.head_sha,
        "branch": snap.manifest.branch,
        "embed_model": snap.manifest.embed_model,
        "embed_dim": snap.manifest.embed_dim,
        "created_at": snap.manifest.created_at,
        "created_by": snap.manifest.created_by,
        "tool_version": snap.manifest.tool_version,
        "counts": snap.manifest.counts,
        "content_sha256": snap.manifest.content_sha256,
    }
    created = store.write(head, data, manifest=manifest_dict)
    result.publish = created
    if created:
        result.notes.append(f"published snapshot {head[:12]}")


def _load_and_verify(blob: bytes, slug: str) -> Snapshot:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".cmsnap", delete=False) as tmp:
        tmp.write(blob)
        path = Path(tmp.name)
    try:
        snap = Snapshot.read(path)
    finally:
        path.unlink(missing_ok=True)
    cfg = CONFIG.for_project(slug)
    res = verify_snapshot(
        snap=snap,
        expected_model=cfg.embed_model,
        expected_dim=cfg.embed_dim,
    )
    if not res.ok:
        raise RuntimeError(f"snapshot verification failed: {res.reason}")
    return snap


def _find_ancestor_snapshot(
    root: Path, store: SnapshotStore, head: str, *, max_depth: int = 200
) -> str | None:
    """Walk back from HEAD on first-parent looking for a published snapshot."""
    available = {e.sha for e in store.list_local()} | {e.sha for e in store.list_remote()}
    if not available:
        return None
    import subprocess

    out = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-list",
            "--first-parent",
            f"-n{max_depth}",
            head,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return None
    for sha in out.stdout.splitlines():
        sha = sha.strip()
        if sha in available:
            return sha
    return None
