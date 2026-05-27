from __future__ import annotations

import json
import os
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


def _resolve_or_none(resolver: Any, text: str) -> str | None:
    """Run entity resolution defensively; swallow any failure to None.

    The CLI's extract-claims path runs from a detached hook process —
    we'd rather store a claim with a NULL entity ID than crash the whole
    extraction because Qdrant blipped or Ollama timed out.
    """
    if resolver is None:
        return None
    try:
        ref = resolver.resolve(text)
    except Exception:  # noqa: BLE001
        return None
    return ref.id if ref is not None else None


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
    no_vectors: bool = typer.Option(
        False,
        "--no-vectors",
        help=(
            "Skip embedding + vector store writes. Builds only the symbol "
            "graph (callers/definitions/importers still work; semantic "
            "retrieve will be empty). Drops Ollama from the critical path "
            "— large repos that don't need semantic recall finish in a "
            "fraction of the time."
        ),
    ),
    as_json: bool = JsonOpt,
) -> None:
    """Ingest a repository.

    Default: git-aware incremental — diff prior state to HEAD.
    """
    slug = project or detect_project_slug(root)
    pipe = Pipeline(project=slug, skip_vectors=no_vectors)
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


@app.command("record-read")
def record_read(
    tool: str = typer.Option(..., "--tool", help="Filesystem tool name (grep, read, bash, glob)"),
    path: str = typer.Option("", "--path", help="File path or pattern accessed"),
    chars: int = typer.Option(0, "--chars", help="Output character count"),
    session_id: str = typer.Option("", "--session-id"),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Record a filesystem read for MCP efficiency tracking.

    Fire-and-forget metrics call — best-effort, never crashes.
    Only persists when CODEMEMORY_METRICS_DB is set.
    """
    db_path = os.environ.get("CODEMEMORY_METRICS_DB") or str(CONFIG.data_dir / "metrics.db")
    try:
        from .metrics import MetricsStore

        ms = MetricsStore(Path(db_path))
        ms.record_fs_read(
            tool=tool,
            path=path,
            project=project or "",
            output_chars=chars,
            session_id=session_id,
        )
        _emit({"recorded": True}, as_json=as_json)
    except Exception as exc:
        _emit({"recorded": False, "error": str(exc)}, as_json=as_json)


@app.command("dedupe-episodes")
def dedupe_episodes(
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Compact duplicate episodes by prompt hash, prune their vectors.

    Same prompt asserted N times collapses to one row whose ts is the
    most-recent observation. Matching Qdrant points are deleted so the
    vector store stays aligned with SQLite.
    """
    pipe = Pipeline(project=project)
    result = pipe.dedupe_episodes()
    _emit({"project": pipe.slug, **result}, as_json=as_json)


@app.command("extract-claims")
def extract_claims(
    prompt: list[str] = typer.Option(
        [],
        "--prompt",
        "-p",
        help="User prompt text. Repeat for multiple prompts.",
    ),
    session_id: str = typer.Option("", "--session-id"),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Run Graphiti-style claim extraction over user prompts.

    Honors ``CLAIMS_EXTRACTION`` env var: when disabled, emits a
    ``{"status": "disabled"}`` payload and exits 0 (callers can treat
    this as a no-op).
    """
    from .claims import ClaimExtractor, ClaimRecord, ClaimsStore, EntityResolver
    from .claims.extractor import ExtractionError
    from .config import CONFIG
    from .orchestrator import git_delta
    import time as _t

    if not CONFIG.claims_enabled:
        _emit(
            {
                "status": "disabled",
                "hint": (
                    "set CLAIMS_EXTRACTION=true after "
                    "`ollama pull gemma2:9b`."
                ),
            },
            as_json=as_json,
        )
        raise typer.Exit(code=0)

    prompts = [p.strip() for p in prompt if p and p.strip()]
    if not prompts:
        _emit({"claims_added": 0, "claims": []}, as_json=as_json)
        raise typer.Exit(code=0)

    slug = detect_project_slug() if project is None else project
    cfg = CONFIG.for_project(slug)

    repo = Path.cwd()
    head = None
    if git_delta.is_git_repo(repo):
        try:
            head = git_delta.head_sha(repo)
        except git_delta.GitError:
            head = None

    store = ClaimsStore(path=cfg.claims_db)
    extractor = ClaimExtractor()
    # Entity resolution is best-effort: a Qdrant outage shouldn't lose
    # claims, so a None resolver means "skip resolution, persist with
    # NULL entity IDs". The resolver itself constructs lazily and only
    # warms on the first resolve() call.
    resolver: EntityResolver | None
    try:
        resolver = EntityResolver(project=slug, cfg=cfg)
    except Exception:  # noqa: BLE001
        resolver = None
    added = 0
    samples: list[dict[str, object]] = []
    try:
        for text in prompts:
            try:
                claims = extractor.extract(text)
            except ExtractionError as exc:
                _emit(
                    {
                        "error": "ExtractionError",
                        "message": str(exc),
                        "claims_added": added,
                    },
                    as_json=as_json,
                )
                raise typer.Exit(code=0)
            now = _t.time()
            for c in claims:
                subj_id = _resolve_or_none(resolver, c.subject)
                obj_id = _resolve_or_none(resolver, c.object)
                rec = ClaimRecord(
                    subject=c.subject,
                    predicate=c.predicate,
                    object=c.object,
                    polarity=c.polarity,
                    confidence=c.confidence,
                    evidence_span=c.evidence_span,
                    valid_at=now,
                    head_sha=head,
                    session_id=session_id or None,
                    entity_subject_id=subj_id,
                    entity_object_id=obj_id,
                )
                store.upsert(rec)
                added += 1
                if len(samples) < 5:
                    samples.append(
                        {
                            "subject": rec.subject,
                            "predicate": rec.predicate,
                            "object": rec.object,
                            "confidence": rec.confidence,
                        }
                    )
    finally:
        extractor.close()
        store.close()

    _emit(
        {"project": slug, "claims_added": added, "sample": samples},
        as_json=as_json,
    )


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
            "import_aliases_added": r.import_aliases_added,
        },
        as_json=as_json,
    )


def _parse_duration(spec: str) -> float:
    """Parse strings like ``30d`` / ``12h`` / ``45m`` / ``900s`` into seconds."""
    spec = spec.strip().lower()
    if not spec:
        raise typer.BadParameter("duration is empty")
    unit_to_secs = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}
    unit = spec[-1]
    if unit not in unit_to_secs:
        # treat as bare seconds for ergonomics — ``--older-than 600`` works
        try:
            return float(spec)
        except ValueError as e:
            raise typer.BadParameter(
                f"unknown duration unit in {spec!r}; use s/m/h/d/w"
            ) from e
    try:
        value = float(spec[:-1])
    except ValueError as e:
        raise typer.BadParameter(f"could not parse duration {spec!r}") from e
    return value * unit_to_secs[unit]


@app.command()
def vacuum(
    project: str | None = ProjectOpt,
    before: str | None = typer.Option(
        None,
        "--before",
        help=(
            "Drop tombstones invalidated at or before this git ref "
            "(branch / tag / sha). Mutually exclusive with --older-than / --all."
        ),
    ),
    older_than: str | None = typer.Option(
        None,
        "--older-than",
        help=(
            "Drop tombstones older than this duration (e.g. 30d, 12h). "
            "Mutually exclusive with --before / --all."
        ),
    ),
    drop_all: bool = typer.Option(
        False,
        "--all",
        help="Drop every tombstone regardless of age. Mutually exclusive with the other modes.",
    ),
    repo: Path = typer.Option(
        Path("."),
        "--repo",
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="Repo root used to resolve --before refs to topological ordinals.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be removed without writing.",
    ),
    as_json: bool = JsonOpt,
) -> None:
    """Drop tombstoned graph elements to bound monotonic growth.

    Tombstones accumulate because temporal deletes preserve history.
    Once a SHA is "ancient" for your workflow (released, archived, or
    just irrelevant), vacuum reclaims the space.
    """
    modes_set = [
        x is not None and x is not False
        for x in (before, older_than, drop_all or None)
    ]
    if sum(modes_set) != 1:
        raise typer.BadParameter(
            "specify exactly one of --before / --older-than / --all"
        )

    graph = _graph_for(project)
    kwargs: dict[str, Any] = {"dry_run": dry_run}
    payload: dict[str, Any] = {
        "project": project or detect_project_slug(),
        "dry_run": dry_run,
    }

    if before is not None:
        try:
            sha = _git_delta.resolve_ref(repo, before)
        except _git_delta.GitError as e:
            raise typer.BadParameter(f"could not resolve --before {before!r}: {e}") from e
        ord_ = _git_delta.commit_ordinal(repo, sha)
        if ord_ is None:
            raise typer.BadParameter(
                f"could not compute ordinal for {sha} (shallow clone?)"
            )
        kwargs["before_ord"] = ord_
        payload["mode"] = "before"
        payload["before_sha"] = sha
        payload["before_ord"] = ord_
    elif older_than is not None:
        kwargs["older_than_seconds"] = _parse_duration(older_than)
        payload["mode"] = "older_than"
        payload["older_than_seconds"] = kwargs["older_than_seconds"]
    else:
        kwargs["drop_all"] = True
        payload["mode"] = "all"

    result = graph.vacuum(**kwargs)
    payload["removed"] = result
    _emit(payload, as_json=as_json)


@app.command()
def drift(
    project: str | None = ProjectOpt,
    repo: Path = typer.Option(
        Path("."),
        "--repo",
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="Repo root used to read HEAD.",
    ),
    as_json: bool = JsonOpt,
) -> None:
    """List symbols whose ``last_seen_sha`` doesn't match HEAD.

    Useful for sanity-checking a long-running watcher and for surfacing
    references in comments / docs that point at code the most recent
    ingest no longer confirms.
    """
    try:
        head = _git_delta.head_sha(repo)
    except _git_delta.GitError as e:
        raise typer.BadParameter(f"could not read HEAD from {repo}: {e}") from e
    graph = _graph_for(project)
    rows = graph.drift(head)
    _emit(
        {
            "project": project or detect_project_slug(),
            "head_sha": head,
            "count": len(rows),
            "items": rows,
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
def injects(
    symbol: str = typer.Argument(..., help="Symbol whose defining file is inspected."),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """List DI tokens injected by the file that defines ``symbol``."""
    rows = _graph_for(project).injects(symbol)
    _emit({"symbol": symbol, "injects": rows}, as_json=as_json)


@app.command()
def injectors(
    token: str = typer.Argument(..., help="DI token name."),
    project: str | None = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """List files that inject ``token`` (reverse INJECTS edges)."""
    rows = _graph_for(project).injectors(token)
    _emit({"token": token, "injectors": rows}, as_json=as_json)


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
