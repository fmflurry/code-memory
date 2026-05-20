from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

from .config import detect_project_slug
from .episodic import Episode
from .orchestrator import Pipeline, Retriever

app = typer.Typer(no_args_is_help=True, add_completion=False, help="code-memory CLI")


ProjectOpt = typer.Option(
    None,
    "--project",
    "-p",
    help="Project slug for namespaced storage. Auto-detected if omitted.",
)


@app.command()
def ingest(
    root: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    project: str | None = ProjectOpt,
) -> None:
    """Ingest a repository."""
    slug = project or detect_project_slug(root)
    pipe = Pipeline(project=slug)
    stats = pipe.ingest_repo(root)
    rprint({"project": slug, "ingested": stats.__dict__})


@app.command()
def reingest(
    path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False),
    project: str | None = ProjectOpt,
) -> None:
    """Re-ingest a single file."""
    slug = project or detect_project_slug(path)
    pipe = Pipeline(project=slug)
    ex = pipe.reingest_file(path)
    if ex is None:
        rprint("[yellow]Unsupported file type[/]")
        raise typer.Exit(code=1)
    rprint({"project": slug, "path": ex.path, "symbols": len(ex.symbols), "imports": len(ex.imports)})


@app.command()
def retrieve(
    query: str = typer.Argument(...),
    k: int = typer.Option(8, "--k", help="top-k code"),
    eps: int = typer.Option(5, "--eps", help="top-k episodes"),
    project: str | None = ProjectOpt,
) -> None:
    """Retrieve context pack for a natural-language query."""
    r = Retriever(project=project)
    pack = r.retrieve(query, top_k_code=k, top_k_eps=eps)
    rprint(pack.render())


@app.command()
def record(
    prompt: str = typer.Option(..., "--prompt"),
    plan: str = typer.Option("", "--plan"),
    patch: str = typer.Option("", "--patch"),
    verdict: str = typer.Option("", "--verdict"),
    project: str | None = ProjectOpt,
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
    rprint({"project": pipe.slug, "id": ep_id})


@app.command()
def project(
    root: Path | None = typer.Argument(None, exists=True, file_okay=False, dir_okay=True),
) -> None:
    """Print the resolved project slug for ROOT (or cwd)."""
    rprint({"slug": detect_project_slug(root)})


if __name__ == "__main__":
    app()
