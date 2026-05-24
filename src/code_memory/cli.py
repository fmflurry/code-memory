from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer
from rich import print as rprint

from dataclasses import asdict as _asdict

from .config import CONFIG, detect_project_slug
from .episodic import Episode
from .graph import FalkorStore
from .orchestrator import Pipeline, Retriever, list_projects, reset_all, reset_project
from .orchestrator import git_delta as _git_delta


def _graph_for(project: str | None) -> FalkorStore:
    slug = project or detect_project_slug()
    cfg = CONFIG.for_project(slug)
    return FalkorStore(graph_name=cfg.falkor_graph)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="code-memory CLI")


ProjectOpt = typer.Option(
    None,
    "--project",
    "-p",
    help="Project slug for namespaced storage. Auto-detected if omitted.",
)

JsonOpt = typer.Option(
    False,
    "--json",
    help="Emit machine-readable JSON to stdout instead of rich output.",
)


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(payload, default=str))
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        rprint(payload)


@app.command()
def ingest(
    root: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    project: str | None = ProjectOpt,
    full: bool = typer.Option(
        False, "--full", help="Force a full walk; ignore stored state."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Base ref (branch/tag/sha) to diff against HEAD."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be ingested; don't write."
    ),
    as_json: bool = JsonOpt,
) -> None:
    """Ingest a repository.

    Default: git-aware incremental — diff prior state to HEAD.
    """
    slug = project or detect_project_slug(root)
    pipe = Pipeline(project=slug)
    stats = pipe.ingest_repo(
        root,
        mode="full" if full else "auto",
        since=since,
        dry_run=dry_run,
    )
    _emit(
        {"project": slug, "dry_run": dry_run, "ingested": asdict(stats)},
        as_json=as_json,
    )


@app.command("ingest-status")
def ingest_status(
    root: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, dir_okay=True
    ),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Show stored ingest state for ROOT (last commit, branch, drift vs HEAD)."""
    slug = project or detect_project_slug(root)
    pipe = Pipeline(project=slug)
    prior = pipe.state.get(root)
    payload: dict[str, object] = {"project": slug, "repo_root": str(Path(root).resolve())}
    if prior is None:
        payload["state"] = None
    else:
        payload["state"] = {
            "last_sha": prior.last_sha,
            "last_ts": prior.last_ts,
            "branch": prior.branch,
        }

    if _git_delta.is_git_repo(root):
        try:
            head = _git_delta.head_sha(root)
            branch = _git_delta.current_branch(root)
            payload["head_sha"] = head
            payload["branch"] = branch
            if prior is not None and _git_delta.is_reachable(root, prior.last_sha):
                d = _git_delta.diff(root, prior.last_sha, head)
                payload["drift"] = {
                    "changed": len(d.changed),
                    "deleted": len(d.deleted),
                }
            payload["dirty"] = len(_git_delta.dirty_files(root))
        except _git_delta.GitError as e:
            payload["git_error"] = str(e)
    else:
        payload["git"] = False

    _emit(payload, as_json=as_json)


@app.command()
def reingest(
    path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Re-ingest a single file."""
    slug = project or detect_project_slug(path)
    pipe = Pipeline(project=slug)
    ex = pipe.reingest_file(path)
    if ex is None:
        _emit({"error": "unsupported file type", "path": str(path)}, as_json=as_json)
        raise typer.Exit(code=1)
    _emit(
        {
            "project": slug,
            "path": ex.path,
            "symbols": len(ex.symbols),
            "imports": len(ex.imports),
        },
        as_json=as_json,
    )


@app.command()
def retrieve(
    query: str = typer.Argument(...),
    k: int = typer.Option(8, "--k", help="top-k code"),
    eps: int = typer.Option(5, "--eps", help="top-k episodes"),
    include_idle_episodes: bool = typer.Option(
        False,
        "--include-idle-episodes",
        help="Include episodes with verdict='idle' (suppressed by default).",
    ),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Retrieve context pack for a natural-language query."""
    r = Retriever(project=project)
    pack = r.retrieve(
        query,
        top_k_code=k,
        top_k_eps=eps,
        include_idle_episodes=include_idle_episodes,
    )
    if as_json:
        _emit(pack.to_dict(), as_json=True)
    else:
        rprint(pack.render())


@app.command()
def record(
    prompt: str = typer.Option(..., "--prompt"),
    plan: str = typer.Option("", "--plan"),
    patch: str = typer.Option("", "--patch"),
    verdict: str = typer.Option("", "--verdict"),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Record a task episode."""
    pipe = Pipeline(project=project)
    ep = Episode(
        prompt=prompt,
        plan=plan or None,
        patch=patch or None,
        verdict=verdict or None,
    )
    ep_id = pipe.record_episode(ep)
    _emit({"project": pipe.slug, "id": ep_id}, as_json=as_json)


@app.command()
def project(
    root: Path | None = typer.Argument(None, exists=True, file_okay=False, dir_okay=True),
    as_json: bool = JsonOpt,
) -> None:
    """Print the resolved project slug for ROOT (or cwd)."""
    _emit({"slug": detect_project_slug(root)}, as_json=as_json)


@app.command()
def projects(as_json: bool = JsonOpt) -> None:
    """List every project slug known to the storage backends."""
    _emit({"projects": list_projects()}, as_json=as_json)


@app.command()
def reset(
    root: Path | None = typer.Argument(
        None,
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="Path used to auto-detect the project slug. Ignored with --all.",
    ),
    project: str | None = ProjectOpt,
    all_: bool = typer.Option(
        False, "--all", help="Wipe every project (use with care)."
    ),
    include_episodes: bool = typer.Option(
        False,
        "--include-episodes",
        help="Also drop episodic memory (conversation history). Destructive.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompt."
    ),
    as_json: bool = JsonOpt,
) -> None:
    """Erase code-index data for a project (or every project).

    Default scope: Qdrant code collection + FalkorDB graph + ingest_state.
    Episodes (conversation memory) are preserved unless --include-episodes.
    """
    if all_:
        targets = list_projects()
        scope_desc = f"all {len(targets)} projects"
    else:
        slug = project or detect_project_slug(root)
        targets = [slug]
        scope_desc = f"project '{slug}'"

    if not targets:
        _emit({"reset": [], "note": "nothing to reset"}, as_json=as_json)
        return

    if not yes:
        extra = " + episodes" if include_episodes else ""
        confirm = typer.confirm(
            f"Reset {scope_desc}{extra}? This drops vectors + graph + ingest_state.",
            default=False,
        )
        if not confirm:
            raise typer.Exit(code=1)

    if all_:
        results = reset_all(include_episodes=include_episodes)
    else:
        results = [
            reset_project(s, include_episodes=include_episodes) for s in targets
        ]

    _emit(
        {"reset": [asdict(r) for r in results]},
        as_json=as_json,
    )


@app.command()
def resolve(
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Re-run the symbol resolver against the current graph.

    Use after writes that mutated cross-file call relationships (rename,
    move, delete). Cheaper than a full re-ingest because it skips
    tree-sitter and embedding — it only re-points placeholder CALLS
    edges to real Symbol nodes.
    """
    from .orchestrator.resolver import resolve_graph

    pipe = Pipeline(project=project)
    r = resolve_graph(pipe.graph)
    _emit(
        {
            "project": pipe.slug,
            "placeholders": r.placeholders,
            "edges_total": r.edges_total,
            "resolved_same_file": r.edges_resolved_same_file,
            "resolved_imported": r.edges_resolved_imported,
            "resolved_unique": r.edges_resolved_unique,
            "ambiguous": r.edges_left_ambiguous,
            "external": r.edges_left_external,
            "placeholders_deleted": r.placeholders_deleted,
        },
        as_json=as_json,
    )


@app.command()
def callers(
    symbol: str = typer.Argument(..., help="Symbol name to look up callers for."),
    depth: int = typer.Option(1, "--depth", help="Traversal depth (1-3)."),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """List files that call a symbol (reverse CALLS edges)."""
    rows = _graph_for(project).callers(symbol, depth=depth)
    _emit({"symbol": symbol, "callers": rows}, as_json=as_json)


@app.command()
def callees(
    symbol: str = typer.Argument(..., help="Symbol name to look up callees for."),
    depth: int = typer.Option(1, "--depth", help="Traversal depth (1-3)."),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """List symbols called from the file that defines ``symbol``."""
    rows = _graph_for(project).callees(symbol, depth=depth)
    _emit({"symbol": symbol, "callees": rows}, as_json=as_json)


@app.command()
def importers(
    target: str = typer.Argument(..., help="Module / package / path."),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """List files that import a module or package."""
    rows = _graph_for(project).importers(target)
    _emit({"target": target, "importers": rows}, as_json=as_json)


@app.command()
def dependencies(
    file: str = typer.Argument(..., help="Absolute file path."),
    depth: int = typer.Option(1, "--depth", help="Traversal depth (1-3)."),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """List modules imported by a file (forward IMPORTS edges)."""
    rows = _graph_for(project).dependencies(file, depth=depth)
    _emit({"file": file, "dependencies": rows}, as_json=as_json)


@app.command()
def definitions(
    symbol: str = typer.Argument(..., help="Symbol name to locate."),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """List all files+line ranges that define ``symbol``."""
    rows = _graph_for(project).definitions(symbol)
    _emit({"symbol": symbol, "definitions": rows}, as_json=as_json)


# ---------------------------------------------------------------------------
# Team sync (snapshot + watcher + autostart + hooks)
# ---------------------------------------------------------------------------


snapshot_app = typer.Typer(help="Snapshot management (publish, list, gc).")
hooks_app = typer.Typer(help="Git hooks installer.")
autostart_app = typer.Typer(help="Cross-platform autostart service.")
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(hooks_app, name="hooks")
app.add_typer(autostart_app, name="autostart")


@app.command()
def sync(
    root: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, dir_okay=True, help="Repo root."
    ),
    project: str | None = ProjectOpt,
    publish: bool = typer.Option(
        False,
        "--publish",
        help="If on the canonical branch, publish a fresh snapshot after sync.",
    ),
    canonical_branch: str = typer.Option(
        "main", "--canonical-branch", help="Branch whose tip publishes snapshots."
    ),
    trigger: str = typer.Option(
        "manual", "--trigger", help="Free-form tag (e.g. post-merge, watcher)."
    ),
    no_fetch: bool = typer.Option(
        False, "--no-fetch", help="Skip `git fetch` of the snapshot branch."
    ),
    as_json: bool = JsonOpt,
) -> None:
    """Reconcile local code-memory state with git HEAD.

    Pulls a snapshot if one exists for HEAD or a recent ancestor,
    otherwise runs an incremental ingest. Idempotent: cheap on
    quiet repos, fast on small diffs, falls back to a full ingest
    only when nothing else is available.
    """
    from .sync import sync_repo

    result = sync_repo(
        root,
        project=project,
        publish=publish,
        canonical_branch=canonical_branch,
        trigger=trigger,
        fetch=not no_fetch,
    )
    _emit(_asdict(result), as_json=as_json)


@app.command()
def watch(
    root: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, dir_okay=True, help="Repo root."
    ),
    project: str | None = ProjectOpt,
) -> None:
    """Run the filesystem watcher in the foreground until interrupted."""
    from .sync.safety import UnsafeWatchRootError, assert_safe_watch_root
    from .sync.watcher import run_foreground

    try:
        safe_root = assert_safe_watch_root(root)
    except UnsafeWatchRootError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=2) from e

    run_foreground(safe_root, project=project)


@app.command()
def status(
    root: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, dir_okay=True, help="Repo root."
    ),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Show a unified sync status (autostart, hooks, snapshot, drift)."""
    from .sync.autostart import ensure_autostart  # noqa: F401 - imported for side-types
    from .sync.autostart.base import get_adapter
    from .sync.hooks import hook_status
    from .sync.store import SnapshotStore

    slug = project or detect_project_slug(root)
    payload: dict[str, object] = {"project": slug, "root": str(Path(root).resolve())}

    # autostart
    try:
        adapter = get_adapter()
        st = adapter.status(Path(root).resolve())
        payload["autostart"] = {
            "installed": st.installed,
            "running": st.running,
            "label": st.label,
            "unit_path": st.unit_path,
            "note": st.note,
        }
    except Exception as e:  # noqa: BLE001
        payload["autostart"] = {"error": str(e)}

    # hooks
    payload["hooks"] = hook_status(Path(root).resolve())

    # snapshot drift
    try:
        if _git_delta.is_git_repo(root):
            head = _git_delta.head_sha(root)
            store = SnapshotStore(Path(root).resolve())
            store.fetch()
            payload["head_sha"] = head
            payload["snapshot_for_head"] = store.has(head)
            payload["local_snapshots"] = len(store.list_local())
            payload["remote_snapshots"] = len(store.list_remote())
    except Exception as e:  # noqa: BLE001
        payload["snapshot_error"] = str(e)

    # ingest state
    try:
        cfg = CONFIG.for_project(slug)
        from .orchestrator.ingest_state import IngestStateStore

        prior = IngestStateStore(cfg.episodic_db).get(root)
        payload["ingest_state"] = (
            None
            if prior is None
            else {"last_sha": prior.last_sha, "branch": prior.branch, "last_ts": prior.last_ts}
        )
    except Exception as e:  # noqa: BLE001
        payload["ingest_state_error"] = str(e)

    _emit(payload, as_json=as_json)


# ---- snapshot subcommands -------------------------------------------------


@snapshot_app.command("publish")
def snapshot_publish(
    root: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, dir_okay=True
    ),
    project: str | None = ProjectOpt,
    push: bool = typer.Option(True, "--push/--no-push", help="Push the snapshot branch."),
    as_json: bool = JsonOpt,
) -> None:
    """Build a snapshot for HEAD and push it to the snapshot branch."""
    from .sync.snapshot import build_snapshot
    from .sync.store import SnapshotStore

    if not _git_delta.is_git_repo(root):
        _emit({"error": "not a git repo"}, as_json=as_json)
        raise typer.Exit(code=1)
    slug = project or detect_project_slug(root)
    head = _git_delta.head_sha(root)
    branch = _git_delta.current_branch(root)
    snap = build_snapshot(
        project=slug,
        head_sha=head,
        branch=branch,
        state={"last_sha": head, "branch": branch},
    )
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".cmsnap", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        snap.write(tmp_path)
        data = tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)
    store = SnapshotStore(Path(root).resolve())
    manifest: dict[str, object] = {
        "head_sha": head,
        "branch": branch,
        "size": len(data),
        "embed_model": snap.manifest.embed_model,
        "embed_dim": snap.manifest.embed_dim,
        "counts": snap.manifest.counts,
        "content_sha256": snap.manifest.content_sha256,
    }
    created = store.write(head, data, manifest=manifest, push=push)
    _emit(
        {
            "project": slug,
            "head": head,
            "created": created,
            "size": len(data),
            "counts": snap.manifest.counts,
        },
        as_json=as_json,
    )


@snapshot_app.command("list")
def snapshot_list(
    root: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, dir_okay=True
    ),
    remote_only: bool = typer.Option(False, "--remote", help="Only list remote entries."),
    as_json: bool = JsonOpt,
) -> None:
    """List snapshots present on the snapshot branch."""
    from .sync.store import SnapshotStore

    store = SnapshotStore(Path(root).resolve())
    store.fetch()
    rows = store.list_remote() if remote_only else store.list_local()
    _emit({"snapshots": [_asdict(r) for r in rows]}, as_json=as_json)


@snapshot_app.command("gc")
def snapshot_gc(
    root: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, dir_okay=True
    ),
    keep: int = typer.Option(20, "--keep", help="Number of recent snapshots to keep."),
    push: bool = typer.Option(True, "--push/--no-push"),
    as_json: bool = JsonOpt,
) -> None:
    """Prune all but the most recent ``--keep`` snapshots."""
    from .sync.store import SnapshotStore

    store = SnapshotStore(Path(root).resolve())
    removed = store.gc(keep, push=push)
    _emit({"removed": removed, "kept": keep}, as_json=as_json)


# ---- hooks subcommands ----------------------------------------------------


@hooks_app.command("install")
def hooks_install(
    root: Path = typer.Argument(Path("."), exists=True, file_okay=False, dir_okay=True),
    with_autostart: bool = typer.Option(
        True, "--autostart/--no-autostart", help="Also register OS autostart."
    ),
    as_json: bool = JsonOpt,
) -> None:
    """Install git hooks (and OS autostart) for this repo."""
    from .sync.hooks import install_hooks

    result = install_hooks(Path(root).resolve())
    payload: dict[str, object] = {
        "hooks_dir": result.hooks_dir,
        "installed": result.installed,
        "skipped": result.skipped,
    }
    if with_autostart:
        try:
            from .sync.autostart import ensure_autostart

            st = ensure_autostart(Path(root).resolve())
            payload["autostart"] = {
                "installed": st.installed,
                "running": st.running,
                "label": st.label,
                "unit_path": st.unit_path,
                "note": st.note,
            }
        except Exception as e:  # noqa: BLE001
            payload["autostart_error"] = str(e)
    _emit(payload, as_json=as_json)


@hooks_app.command("uninstall")
def hooks_uninstall(
    root: Path = typer.Argument(Path("."), exists=True, file_okay=False, dir_okay=True),
    with_autostart: bool = typer.Option(True, "--autostart/--no-autostart"),
    as_json: bool = JsonOpt,
) -> None:
    """Remove code-memory git hooks (and OS autostart)."""
    from .sync.hooks import uninstall_hooks

    result = uninstall_hooks(Path(root).resolve())
    payload: dict[str, object] = {
        "removed": result.installed,
        "skipped": result.skipped,
    }
    if with_autostart:
        try:
            from .sync.autostart.base import get_adapter

            st = get_adapter().uninstall(Path(root).resolve())
            payload["autostart"] = {
                "installed": st.installed,
                "label": st.label,
            }
        except Exception as e:  # noqa: BLE001
            payload["autostart_error"] = str(e)
    _emit(payload, as_json=as_json)


# ---- autostart subcommands ------------------------------------------------


@autostart_app.command("install")
def autostart_install(
    root: Path = typer.Argument(Path("."), exists=True, file_okay=False, dir_okay=True),
    as_json: bool = JsonOpt,
) -> None:
    """Register the OS-level autostart service."""
    from .sync.autostart import ensure_autostart

    st = ensure_autostart(Path(root).resolve())
    _emit(
        {
            "installed": st.installed,
            "running": st.running,
            "label": st.label,
            "unit_path": st.unit_path,
            "note": st.note,
        },
        as_json=as_json,
    )


@autostart_app.command("uninstall")
def autostart_uninstall(
    root: Path = typer.Argument(Path("."), exists=True, file_okay=False, dir_okay=True),
    as_json: bool = JsonOpt,
) -> None:
    """Remove the OS-level autostart service."""
    from .sync.autostart.base import get_adapter

    st = get_adapter().uninstall(Path(root).resolve())
    _emit({"installed": st.installed, "label": st.label}, as_json=as_json)


@autostart_app.command("status")
def autostart_status(
    root: Path = typer.Argument(Path("."), exists=True, file_okay=False, dir_okay=True),
    as_json: bool = JsonOpt,
) -> None:
    """Show OS autostart status for this repo."""
    from .sync.autostart.base import get_adapter

    st = get_adapter().status(Path(root).resolve())
    _emit(
        {
            "installed": st.installed,
            "running": st.running,
            "label": st.label,
            "unit_path": st.unit_path,
            "note": st.note,
        },
        as_json=as_json,
    )


if __name__ == "__main__":
    app()
