from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer
from rich import print as rprint

from .config import detect_project_slug
from .episodic import Episode
from .orchestrator import Pipeline, Retriever, list_projects, reset_all, reset_project
from .orchestrator import git_delta as _git_delta

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


if __name__ == "__main__":
    app()
