from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

from .episodic import Episode
from .orchestrator import Pipeline, Retriever

app = typer.Typer(no_args_is_help=True, add_completion=False, help="code-memory CLI")


@app.command()
def ingest(
    root: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
) -> None:
    """Ingest a repository."""
    pipe = Pipeline()
    stats = pipe.ingest_repo(root)
    rprint({"ingested": stats.__dict__})


@app.command()
def reingest(path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False)) -> None:
    """Re-ingest a single file."""
    pipe = Pipeline()
    ex = pipe.reingest_file(path)
    if ex is None:
        rprint("[yellow]Unsupported file type[/]")
        raise typer.Exit(code=1)
    rprint({"path": ex.path, "symbols": len(ex.symbols), "imports": len(ex.imports)})


@app.command()
def retrieve(
    query: str = typer.Argument(...),
    k: int = typer.Option(8, "--k", help="top-k code"),
    eps: int = typer.Option(5, "--eps", help="top-k episodes"),
) -> None:
    """Retrieve context pack for a natural-language query."""
    r = Retriever()
    pack = r.retrieve(query, top_k_code=k, top_k_eps=eps)
    rprint(pack.render())


@app.command()
def record(
    prompt: str = typer.Option(..., "--prompt"),
    plan: str = typer.Option("", "--plan"),
    patch: str = typer.Option("", "--patch"),
    verdict: str = typer.Option("", "--verdict"),
) -> None:
    """Record a task episode."""
    pipe = Pipeline()
    ep = Episode(
        prompt=prompt,
        plan=plan or None,
        patch=patch or None,
        verdict=verdict or None,
    )
    ep_id = pipe.record_episode(ep)
    rprint({"id": ep_id})


if __name__ == "__main__":
    app()
