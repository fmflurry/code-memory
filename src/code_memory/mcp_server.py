"""MCP server exposing code-memory as native tools for coding agents.

Tools:
  - codememory_retrieve(query, k?, eps?, project?)
  - codememory_record(prompt, plan?, patch?, verdict?, project?)
  - codememory_reingest(path, project?)

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

from .episodic import Episode
from .orchestrator import Pipeline, Retriever

SERVER_NAME = "code-memory"

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
                "project": {
                    "type": "string",
                    "description": "Project slug. Auto-detected from cwd if omitted.",
                },
            },
            "required": ["query"],
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
                "project": {"type": "string"},
            },
            "required": ["prompt"],
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
                "project": {"type": "string"},
            },
            "required": ["path"],
        },
    ),
]


def _text(payload: Any) -> list[TextContent]:
    if isinstance(payload, str):
        return [TextContent(type="text", text=payload)]
    return [TextContent(type="text", text=json.dumps(payload, default=str, indent=2))]


def _retrieve(args: dict[str, Any]) -> list[TextContent]:
    query = args["query"]
    k = int(args.get("k", 8))
    eps = int(args.get("eps", 5))
    project = args.get("project")
    include_idle = bool(args.get("include_idle_episodes", False))
    pack = Retriever(project=project).retrieve(
        query,
        top_k_code=k,
        top_k_eps=eps,
        include_idle_episodes=include_idle,
    )
    return _text(pack.render())


def _record(args: dict[str, Any]) -> list[TextContent]:
    project = args.get("project")
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
    path = Path(args["path"])
    if not path.exists() or not path.is_file():
        return _text({"error": f"not a file: {path}"})
    project = args.get("project")
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


_HANDLERS = {
    "codememory_retrieve": _retrieve,
    "codememory_record": _record,
    "codememory_reingest": _reingest,
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
