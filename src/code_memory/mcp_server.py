"""MCP server exposing code-memory as native tools for coding agents.

Tools:
  - codememory_retrieve(query, k?, eps?, project?)         — orientation
  - codememory_record(prompt, plan?, patch?, verdict?, project?)
  - codememory_reingest(path, project?)
  - codememory_ingest(root, project, full?, since?, dry_run?, confirmed?)
  - codememory_callers(symbol, depth?, project?)           — topology
  - codememory_callees(symbol, depth?, project?)
  - codememory_importers(target, project?)
  - codememory_dependencies(file, depth?, project?)
  - codememory_definitions(symbol, project?)

Transport: stdio. Register via `code-memory-mcp` script entrypoint.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from dataclasses import asdict

from .config import CONFIG, detect_project_slug
from .episodic import Episode
from .graph import FalkorStore
from .orchestrator import Pipeline, Retriever
from .orchestrator.pipeline import IngestMode

log = logging.getLogger("codememory.mcp")

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
        name="codememory_ingest",
        description=(
            "LONG-RUNNING / BLOCKING. Ingest an entire repository. "
            "DO NOT call without first asking the user for confirmation — full "
            "ingests can take minutes to hours on large repos and block the "
            "MCP transport while running. Default mode is git-incremental "
            "(diff prior state to HEAD); pass `full=true` to purge this "
            "project's vectors+graph+ingest_state and re-walk every file. "
            "Once the user has explicitly confirmed, call again with "
            "`confirmed=true` to actually run. Without `confirmed=true` the "
            "server returns a dry advisory payload describing what would run."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": "Absolute path to the repo root to ingest.",
                },
                "project": _project_schema(),
                "full": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, purge this project's storage and walk every "
                        "file. Equivalent to CLI `ingest --full`."
                    ),
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Optional base ref (branch/tag/sha) to diff against "
                        "HEAD. Overrides stored ingest state when set."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "Compute plan only; do not write to storage.",
                },
                "confirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Must be true to actually run. Set only after the "
                        "user explicitly authorized this ingest in chat."
                    ),
                },
            },
            "required": ["root", "project"],
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
            "('@acme-ng/security', 'rxjs') or a relative path that was "
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
    Tool(
        name="codememory_assembly_members",
        description=(
            "List the public methods declared on a Type from an indexed .NET "
            "Assembly. Members are NOT bulk-indexed (would multiply the graph "
            "by 50-100x for a typical solution); this tool reads them on-demand "
            "from the DLL when the agent needs to disambiguate an overload or "
            "look up an API surface. Same DLL may be parsed multiple times — "
            "fast enough for interactive use (~tens of ms)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": (
                        "Fully qualified type name (Namespace.Name). Run "
                        "codememory_definitions on the bare name first if "
                        "you're unsure which assembly exposes it."
                    ),
                },
                "assembly": {
                    "type": "string",
                    "description": (
                        "Optional assembly identity ('Name, Version=X.Y.Z.W'). "
                        "When omitted, the first matching assembly wins."
                    ),
                },
                "project": _project_schema(),
            },
            "required": ["type", "project"],
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


def _ensure_fresh(project: str) -> None:
    """Pre-query guard: sync the active repo if HEAD has drifted.

    Cheap no-op when state already matches HEAD and the worktree is
    clean. Skipped entirely when ``CODE_MEMORY_NO_GUARD`` is set.
    """
    if os.environ.get("CODE_MEMORY_NO_GUARD"):
        return
    repo = Path(os.environ.get("CODE_MEMORY_REPO") or os.getcwd()).resolve()
    if not (repo / ".git").exists():
        return
    try:
        from .sync import sync_repo

        sync_repo(repo, project=project, trigger="pre-query", fetch=False)
    except Exception:  # noqa: BLE001
        log.exception("pre-query guard sync failed")


def _retrieve(args: dict[str, Any]) -> list[TextContent]:
    project = _require_project(args)
    _ensure_fresh(project)
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


def _ingest(args: dict[str, Any]) -> list[TextContent]:
    project = _require_project(args)
    raw_root = args.get("root")
    if not isinstance(raw_root, str) or not raw_root.strip():
        return _text({"error": "`root` is required (absolute repo path)."})
    root = Path(raw_root).expanduser()
    if not root.exists() or not root.is_dir():
        return _text({"error": f"not a directory: {root}"})

    full = bool(args.get("full", False))
    since = args.get("since") or None
    dry_run = bool(args.get("dry_run", False))
    confirmed = bool(args.get("confirmed", False))
    mode: IngestMode = "full" if full else "auto"

    if not confirmed:
        return _text(
            {
                "status": "confirmation_required",
                "project": project,
                "root": str(root.resolve()),
                "mode": mode,
                "since": since,
                "dry_run": dry_run,
                "warning": (
                    "LONG-RUNNING / BLOCKING operation. Ask the user to "
                    "confirm before re-invoking with `confirmed=true`. "
                    "Full ingests can take minutes to hours."
                ),
            }
        )

    pipe = Pipeline(project=project)
    stats = pipe.ingest_repo(root, mode=mode, since=since, dry_run=dry_run)
    return _text(
        {
            "project": pipe.slug,
            "root": str(root.resolve()),
            "mode": mode,
            "dry_run": dry_run,
            "ingested": asdict(stats),
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


def _assembly_members(args: dict[str, Any]) -> list[TextContent]:
    """List the public methods of one Type from an indexed Assembly.

    Members aren't bulk-indexed (a NuGet pkg can expose 10k+ of them).
    This tool reads them on-demand directly from the DLL when the
    agent needs to disambiguate an overload or look up an API surface.
    """
    from .extractor.dll import parse_type_members

    g, slug = _graph_for(_require_project(args))
    type_arg = args.get("type")
    if not isinstance(type_arg, str) or not type_arg:
        return _text({"error": "ValueError", "message": "type is required"})

    namespace, _, name = type_arg.rpartition(".")
    if not name:
        return _text(
            {"error": "ValueError", "message": "type must be fully qualified"}
        )

    asm_filter = args.get("assembly")
    cypher = (
        "MATCH (a:Assembly)-[:EXPOSES_TYPE]->(t:Type) "
        "WHERE t.name = $name AND t.namespace = $ns"
    )
    params: dict[str, Any] = {"name": name, "ns": namespace}
    if isinstance(asm_filter, str) and asm_filter:
        cypher += " AND a.key = $asm"
        params["asm"] = asm_filter
    cypher += " RETURN a.key, a.path"

    rows = g.graph.query(cypher, params).result_set
    if not rows:
        return _text(
            {
                "project": slug,
                "type": type_arg,
                "assembly": asm_filter,
                "error": "type not found in indexed assemblies",
            }
        )

    for asm_key, asm_path in rows:
        members = parse_type_members(asm_path, namespace, name)
        if members is None:
            continue
        return _text(
            {
                "project": slug,
                "type": type_arg,
                "assembly": asm_key,
                "count": len(members),
                "members": [
                    {
                        "name": m.name,
                        "kind": m.kind,
                        "static": m.static,
                        "params": m.params,
                    }
                    for m in members
                ],
            }
        )

    return _text(
        {
            "project": slug,
            "type": type_arg,
            "error": "no parsable DLL found for the type's assemblies",
        }
    )


_HANDLERS = {
    "codememory_retrieve": _retrieve,
    "codememory_record": _record,
    "codememory_reingest": _reingest,
    "codememory_ingest": _ingest,
    "codememory_callers": _callers,
    "codememory_callees": _callees,
    "codememory_importers": _importers,
    "codememory_dependencies": _dependencies,
    "codememory_definitions": _definitions,
    "codememory_assembly_members": _assembly_members,
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


def _bootstrap_repo() -> Path | None:
    """Locate the active repo and ensure autostart + in-process watcher.

    Best-effort: any failure (no git, no write permission, missing
    watchdog dep) logs and continues. The MCP server still serves
    queries even if these side-channels can't be set up.
    """
    candidate = os.environ.get("CODE_MEMORY_REPO") or os.getcwd()
    repo = Path(candidate).resolve()
    if not (repo / ".git").exists():
        # try git toplevel
        from .orchestrator import git_delta

        if git_delta.is_git_repo(repo):
            try:
                import subprocess

                top = subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2,
                ).stdout.strip()
                if top:
                    repo = Path(top)
            except Exception:  # noqa: BLE001
                pass
    if not (repo / ".git").exists():
        log.info("mcp bootstrap: not a git repo (%s); skipping autostart", repo)
        return None

    # 1. autostart registration (idempotent)
    if not os.environ.get("CODE_MEMORY_NO_AUTOSTART"):
        try:
            from .sync.autostart import ensure_autostart

            st = ensure_autostart(repo)
            log.info(
                "mcp bootstrap: autostart installed=%s running=%s label=%s",
                st.installed,
                st.running,
                st.label,
            )
        except Exception:  # noqa: BLE001
            log.exception("mcp bootstrap: autostart registration failed")

    # 2. one-shot sync to catch up to HEAD
    if not os.environ.get("CODE_MEMORY_NO_BOOT_SYNC"):
        try:
            from .sync import sync_repo

            result = sync_repo(repo, trigger="mcp-boot")
            log.info(
                "mcp bootstrap: sync action=%s head=%s",
                result.action,
                (result.head_sha or "")[:12],
            )
        except Exception:  # noqa: BLE001
            log.exception("mcp bootstrap: initial sync failed")

    # 3. in-process watcher as belt-and-suspenders (won't double-start
    #    because OS autostart runs in its own process)
    if not os.environ.get("CODE_MEMORY_NO_INPROC_WATCHER"):
        try:
            from .sync.watcher import Watcher

            w = Watcher(repo)
            w.start()
            log.info("mcp bootstrap: in-process watcher started")
            _BOOTSTRAP_REFS["watcher"] = w
        except Exception:  # noqa: BLE001
            log.exception("mcp bootstrap: in-process watcher failed to start")

    return repo


_BOOTSTRAP_REFS: dict[str, Any] = {}


async def _run() -> None:
    _bootstrap_repo()
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("CODE_MEMORY_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    anyio.run(_run)


if __name__ == "__main__":
    main()
