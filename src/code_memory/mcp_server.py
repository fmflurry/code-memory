"""MCP server exposing code-memory as native tools for coding agents.

Tools:
  - codememory_retrieve(query, k?, eps?, project?)         — orientation
  - codememory_record(prompt, plan?, patch?, verdict?, project?)
  - codememory_reingest(path, project?)
  - codememory_callers(symbol, depth?, project?)           — topology
  - codememory_callees(symbol, depth?, project?)
  - codememory_importers(target, project?)
  - codememory_dependencies(file, depth?, project?)
  - codememory_definitions(symbol, project?)

Transport: stdio. Register via `code-memory-mcp` script entrypoint.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .config import CONFIG, detect_project_slug
from .episodic import Episode
from .graph import FalkorStore
from .orchestrator import Pipeline, Retriever

SERVER_NAME = "code-memory"


def _resolved_default_slug() -> str:
    """Best-effort project slug at server startup.

    Surfaced in every tool's ``project`` field description so smaller /
    open-weight models — which often omit optional parameters or invent
    wrong ones — see the *concrete* default and don't need to guess.
    """
    try:
        return detect_project_slug()
    except Exception:
        return "<auto-detect failed>"


_DEFAULT_SLUG = _resolved_default_slug()


def _project_schema() -> dict[str, Any]:
    """Shared schema fragment for the ``project`` field.

    The field is mandatory on every tool — silent cwd-fallback was hiding
    namespace bugs (see commit `6ff8a27`). The description hands the model
    the *exact* slug to pass for the current working directory so it
    doesn't have to guess.
    """
    return {
        "type": "string",
        "description": (
            f"REQUIRED. Project slug for namespaced storage. For this server, "
            f"pass exactly `{_DEFAULT_SLUG}` to query the current project. "
            f"Pass a different slug only when you intentionally want another "
            f"project. Sentinel values like 'auto' or 'default' are rejected."
        ),
    }


_TOOLS: list[Tool] = [
    Tool(
        name="codememory_retrieve",
        description=(
            "Retrieve a context pack (code chunks, past episodes, graph neighbors) "
            "for a natural-language query. Use before editing unfamiliar code."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query."},
                "k": {"type": "integer", "default": 8, "description": "Top-k code chunks."},
                "eps": {"type": "integer", "default": 5, "description": "Top-k episodes."},
                "include_idle_episodes": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include episodes with verdict='idle' (off by default).",
                },
                "project": _project_schema(),
            },
            "required": ["query", "project"],
        },
    ),
    Tool(
        name="codememory_record",
        description=(
            "Record a task episode (prompt + optional plan/patch/verdict). "
            "Call after completing a task so future queries can recall it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "plan": {"type": "string"},
                "patch": {"type": "string"},
                "verdict": {"type": "string", "description": "e.g. 'success', 'reverted'."},
                "project": _project_schema(),
            },
            "required": ["prompt", "project"],
        },
    ),
    Tool(
        name="codememory_reingest",
        description=(
            "Re-index a single file after edits so subsequent retrieval reflects "
            "current state. Call after writing or editing source files."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or cwd-relative file path."},
                "project": _project_schema(),
            },
            "required": ["path", "project"],
        },
    ),
    Tool(
        name="codememory_callers",
        description=(
            "Files that call a symbol. Use for impact analysis ('what breaks "
            "if I rename X?') and to navigate from a definition to its uses."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name (e.g. 'getBearerToken')."},
                "depth": {"type": "integer", "default": 1, "description": "Traversal depth, 1-3."},
                "project": _project_schema(),
            },
            "required": ["symbol", "project"],
        },
    ),
    Tool(
        name="codememory_callees",
        description=(
            "Symbols called from the file that defines a given symbol. "
            "Use to map outgoing dependencies of a service or class."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "depth": {"type": "integer", "default": 1, "description": "Traversal depth, 1-3."},
                "project": _project_schema(),
            },
            "required": ["symbol", "project"],
        },
    ),
    Tool(
        name="codememory_importers",
        description=(
            "Files that import a module or package. Pass a package name "
            "('@internal-ng/security', 'rxjs') or a relative path that was "
            "preserved during ingest ('./bar')."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Module / package / path to look up."},
                "project": _project_schema(),
            },
            "required": ["target", "project"],
        },
    ),
    Tool(
        name="codememory_dependencies",
        description=(
            "Modules imported by a file (forward import graph). Use to "
            "answer 'what does this file depend on?'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute file path."},
                "depth": {"type": "integer", "default": 1, "description": "Traversal depth, 1-3."},
                "project": _project_schema(),
            },
            "required": ["file", "project"],
        },
    ),
    Tool(
        name="codememory_definitions",
        description=(
            "All files+line ranges that define a given symbol name. Use to "
            "disambiguate before calling callers/callees."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "project": _project_schema(),
            },
            "required": ["symbol", "project"],
        },
    ),
]


class MissingProjectError(ValueError):
    """Raised when an MCP tool call omits the required `project` parameter.

    We surface a *helpful* error rather than silently falling back to
    cwd-detection: that fallback was hiding bugs where models invented
    project names or omitted the field entirely, and downstream queries
    were quietly hitting the wrong namespace. See commit `6ff8a27`.
    """

    def __init__(self) -> None:
        super().__init__(
            f"`project` parameter is required. Pass the slug of the project "
            f"you're querying. The server's cwd-detected default is "
            f"`{_DEFAULT_SLUG}` — pass that exact value to use it, or pass "
            f"a different slug to query another project. Use the "
            f"`code-memory projects` CLI to list available slugs."
        )


def _require_project(args: dict[str, Any]) -> str:
    """Return the caller-supplied project slug or raise.

    Sentinel values (``auto``, ``default``, blank) are rejected — those are
    not real slugs and accepting them would re-introduce the silent
    namespace bug we just fixed.
    """
    raw = args.get("project")
    if not isinstance(raw, str):
        raise MissingProjectError()
    slug = raw.strip()
    if not slug or slug.lower() in {"auto", "default"}:
        raise MissingProjectError()
    return slug


def _graph_for(project: str) -> tuple[FalkorStore, str]:
    """Return (graph, resolved_slug). ``project`` must already be validated."""
    cfg = CONFIG.for_project(project)
    return FalkorStore(graph_name=cfg.falkor_graph), project


def _text(payload: Any) -> list[TextContent]:
    if isinstance(payload, str):
        return [TextContent(type="text", text=payload)]
    return [TextContent(type="text", text=json.dumps(payload, default=str, indent=2))]


def _retrieve(args: dict[str, Any]) -> list[TextContent]:
    project = _require_project(args)
    query = args["query"]
    k = int(args.get("k", 8))
    eps = int(args.get("eps", 5))
    include_idle = bool(args.get("include_idle_episodes", False))
    retriever = Retriever(project=project)
    pack = retriever.retrieve(
        query,
        top_k_code=k,
        top_k_eps=eps,
        include_idle_episodes=include_idle,
    )
    return _text(f"_Project: `{retriever.slug}`_\n\n{pack.render()}")


def _record(args: dict[str, Any]) -> list[TextContent]:
    project = _require_project(args)
    pipe = Pipeline(project=project)
    ep = Episode(
        prompt=args["prompt"],
        plan=args.get("plan") or None,
        patch=args.get("patch") or None,
        verdict=args.get("verdict") or None,
    )
    ep_id = pipe.record_episode(ep)
    return _text({"project": pipe.slug, "id": ep_id})


def _reingest(args: dict[str, Any]) -> list[TextContent]:
    project = _require_project(args)
    path = Path(args["path"])
    if not path.exists() or not path.is_file():
        return _text({"error": f"not a file: {path}"})
    pipe = Pipeline(project=project)
    ex = pipe.reingest_file(path)
    if ex is None:
        return _text({"error": "unsupported file type", "path": str(path)})
    return _text(
        {
            "project": pipe.slug,
            "path": ex.path,
            "symbols": len(ex.symbols),
            "imports": len(ex.imports),
        }
    )


def _callers(args: dict[str, Any]) -> list[TextContent]:
    g, slug = _graph_for(_require_project(args))
    rows = g.callers(args["symbol"], depth=int(args.get("depth", 1)))
    return _text({"project": slug, "symbol": args["symbol"], "callers": rows})


def _callees(args: dict[str, Any]) -> list[TextContent]:
    g, slug = _graph_for(_require_project(args))
    rows = g.callees(args["symbol"], depth=int(args.get("depth", 1)))
    return _text({"project": slug, "symbol": args["symbol"], "callees": rows})


def _importers(args: dict[str, Any]) -> list[TextContent]:
    g, slug = _graph_for(_require_project(args))
    rows = g.importers(args["target"])
    return _text({"project": slug, "target": args["target"], "importers": rows})


def _dependencies(args: dict[str, Any]) -> list[TextContent]:
    g, slug = _graph_for(_require_project(args))
    rows = g.dependencies(args["file"], depth=int(args.get("depth", 1)))
    return _text({"project": slug, "file": args["file"], "dependencies": rows})


def _definitions(args: dict[str, Any]) -> list[TextContent]:
    g, slug = _graph_for(_require_project(args))
    rows = g.definitions(args["symbol"])
    return _text({"project": slug, "symbol": args["symbol"], "definitions": rows})


_HANDLERS = {
    "codememory_retrieve": _retrieve,
    "codememory_record": _record,
    "codememory_reingest": _reingest,
    "codememory_callers": _callers,
    "codememory_callees": _callees,
    "codememory_importers": _importers,
    "codememory_dependencies": _dependencies,
    "codememory_definitions": _definitions,
}


def build_server() -> Server:
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return _TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        handler = _HANDLERS.get(name)
        if handler is None:
            return _text({"error": f"unknown tool: {name}"})
        try:
            return await anyio.to_thread.run_sync(lambda: handler(arguments))
        except Exception as exc:  # surface, don't crash the server
            return _text({"error": type(exc).__name__, "message": str(exc)})

    return server


async def _run() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    anyio.run(_run)


if __name__ == "__main__":
    main()
