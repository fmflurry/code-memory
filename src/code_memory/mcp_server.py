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
  - codememory_injects(symbol, project?)                   — Angular/Razor DI
  - codememory_injectors(token, project?)
  - codememory_definitions(symbol, project?)
  - codememory_assembly_members(type, assembly?, project?) — .NET DLL surface
  - codememory_drift(head_sha, project?)                   — temporal
  - codememory_at_sha(sha, sha_ord, label?, limit?, project?)
  - codememory_callers_at_sha(symbol, sha, sha_ord, project?)
  - codememory_extract_claims(prompts, project, session_id?) — Graphiti-style
  - codememory_assert_claim(subject, predicate, object, project, ...) —
    agent-authored direct claim (no LLM)
  - codememory_claims(subject?, as_of?, project)

Transport: stdio. Register via `code-memory-mcp` script entrypoint.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
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
            "for a natural-language query. Use first for codebase/repo/docs "
            "orientation before grep/glob/read, and before editing unfamiliar code."
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
            "DISABLED BY DEFAULT — do NOT call this tool to actually run an "
            "ingest. MCP transport blocks for the full duration of the call "
            "and the host (Claude Code / OpenCode / ...) does not surface "
            "mid-call `notifications/progress` back to the agent, so the user "
            "would see no progress feedback. Instead, run the Bash CLI:\n\n"
            "    code-memory ingest <root> --project <slug>\n\n"
            "Prefer `run_in_background=true` + periodic `BashOutput` polls so "
            "you (the agent) can narrate progress turn-by-turn; the CLI emits "
            "throttled `[code-memory] files=… symbols=… rate=…/s` lines to "
            "stderr every 50 files. The user can `tail -f` the same stream "
            "independently. Calling this MCP tool returns a steering payload "
            "pointing to the CLI. The blocking MCP path can be re-enabled "
            "by setting `CODE_MEMORY_MCP_INGEST_ENABLED=1` in the server env "
            "(then `confirmed=true` is also required)."
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
        name="codememory_injects",
        description=(
            "DI dependencies of a symbol — the tokens its defining file "
            "injects via Angular's ``inject(Token)`` or Razor's ``@inject``. "
            "Use to answer 'what does this use case / service depend on?' "
            "without sifting raw imports. Complements ``codememory_callees``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol whose defining file is the inspection target."},
                "project": _project_schema(),
            },
            "required": ["symbol", "project"],
        },
    ),
    Tool(
        name="codememory_injectors",
        description=(
            "Reverse DI lookup: files that inject a given token. Use to "
            "find every consumer of an Angular DI token / Razor service."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "DI token name (e.g. abstract class used as an Angular port)."},
                "project": _project_schema(),
            },
            "required": ["token", "project"],
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
    Tool(
        name="codememory_drift",
        description=(
            "List symbols whose last_seen_sha doesn't match the supplied "
            "git HEAD — either tombstoned (deleted) or drifted (the most "
            "recent ingest didn't confirm them at HEAD). Use to sanity-check "
            "a long-running watcher or to find stale references in comments."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "head_sha": {
                    "type": "string",
                    "description": (
                        "Git HEAD to compare against. Pass the SHA you consider "
                        "'current' (usually the HEAD of the repo associated "
                        "with this project)."
                    ),
                },
                "project": _project_schema(),
            },
            "required": ["head_sha", "project"],
        },
    ),
    Tool(
        name="codememory_at_sha",
        description=(
            "List Symbol (default) or File nodes that were "
            "alive at the supplied commit. Combine with "
            "codememory_callers_at_sha for 'what called X back then'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sha": {
                    "type": "string",
                    "description": "Full git SHA to query the graph state at.",
                },
                "sha_ord": {
                    "type": "integer",
                    "description": (
                        "Topological ordinal of sha. Server auto-computes "
                        "if omitted."
                    ),
                },
                "sha_ord": {
                    "type": "integer",
                    "description": (
                        "Topological ordinal of sha. Compute on the caller "
                        "side with `git rev-list --count --first-parent <sha>` "
                        "so the MCP server doesn't shell out."
                    ),
                },
                "label": {
                    "type": "string",
                    "enum": ["Symbol", "File"],
                    "default": "Symbol",
                    "description": "Which node label to enumerate.",
                },
                "limit": {
                    "type": "integer",
                    "default": 200,
                    "description": "Maximum rows to return.",
                },
                "project": _project_schema(),
            },
            "required": ["sha", "project"],
        },
    ),
    Tool(
        name="codememory_callers_at_sha",
        description=(
            "Callers of a symbol as the graph looked at the supplied commit. "
            "Answers 'what used to call X before commit Y deleted it' without "
            "a worktree checkout."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "sha": {"type": "string"},
                "sha_ord": {
                    "type": "integer",
                    "description": (
                        "Topological ordinal of sha. Server auto-computes "
                        "if omitted."
                    ),
                },
                "project": _project_schema(),
            },
            "required": ["symbol", "sha", "project"],
        },
    ),
    Tool(
        name="codememory_extract_claims",
        description=(
            "Graphiti-style: extract bi-temporal (subject, predicate, object) "
            "claims from user prompts via a local LLM (gemma2:9b by default) "
            "and store them with valid_at = prompt timestamp, recorded_at = "
            "now, head_sha = current HEAD. Single-valued predicates ('uses', "
            "'prefers', 'deployed-to', ...) close prior conflicting "
            "assertions. On by default. Set CLAIMS_EXTRACTION=false to "
            "disable. Fire-and-forget — call from a Stop hook, not inline "
            "in a turn."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompts": {
                    "type": "array",
                    "description": (
                        "List of user prompts to extract from. Each item "
                        "is either a raw string or "
                        "{text: string, ts?: number, id?: string}."
                    ),
                    "items": {"type": ["string", "object"]},
                },
                "session_id": {
                    "type": "string",
                    "description": "Originating session for provenance.",
                },
                "project": _project_schema(),
            },
            "required": ["prompts", "project"],
        },
    ),
    Tool(
        name="codememory_assert_claim",
        description=(
            "Agent-authored direct claim. Use this when YOU (the agent) "
            "judge that a user message contains a durable assertion worth "
            "remembering across sessions: stable preferences, ownership, "
            "tech-stack decisions, rejections, or explicit corrections of "
            "your behavior. NO LLM is invoked — you supply the structured "
            "triple yourself. Prefer this over codememory_extract_claims "
            "when the assertion is unambiguous; reserve extract_claims for "
            "batch processing of multiple prompts.\n\n"
            "Predicate vocab (kebab-case verbs): `uses`, `prefers`, "
            "`rejected`, `wants-to`, `is-located-at`, `depends-on`, "
            "`deployed-to`, `owns`, `is-a`, `mentioned`, `worked-on`. "
            "Single-valued predicates (uses/prefers/deployed-to/...) "
            "auto-close prior conflicting assertions.\n\n"
            "Worth asserting: 'we use Postgres', 'I prefer terse output', "
            "'don't ship dark mode'. Not worth asserting: questions, "
            "hypotheticals, transient task state, info already in code."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": (
                        "Noun phrase: `user`, `project`, a service name, "
                        "a person, a module."
                    ),
                },
                "predicate": {
                    "type": "string",
                    "description": (
                        "Kebab-case verb phrase. See description for vocab."
                    ),
                },
                "object": {
                    "type": "string",
                    "description": "Noun phrase for what the predicate links to.",
                },
                "polarity": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "True asserts, False negates "
                        "('user does not use X')."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.95,
                    "description": (
                        "How sure are you this is a durable assertion? "
                        "0.95 default since you triaged it yourself."
                    ),
                },
                "evidence_span": {
                    "type": "string",
                    "description": (
                        "Optional verbatim quote from the user message "
                        "that justifies this claim. Recommended for "
                        "auditability."
                    ),
                },
                "valid_at": {
                    "type": "number",
                    "description": (
                        "Optional unix epoch seconds. Defaults to now. "
                        "Set this to the user-message timestamp if "
                        "available."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": "Originating session for provenance.",
                },
                "source_prompt_id": {
                    "type": "string",
                    "description": "Optional ID of the source user prompt.",
                },
                "project": _project_schema(),
            },
            "required": ["subject", "predicate", "object", "project"],
        },
    ),
    Tool(
        name="codememory_claims",
        description=(
            "Read currently-valid user claims (or claims as of a given "
            "world-time). Use to surface user preferences and stated facts "
            "in retrieve packs or to answer 'what did the user say about X?'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Filter by subject (exact match).",
                },
                "as_of": {
                    "type": "number",
                    "description": (
                        "Optional unix epoch seconds; returns claims valid "
                        "at that world-time. Omit for current state."
                    ),
                },
                "limit": {"type": "integer", "default": 50},
                "project": _project_schema(),
            },
            "required": ["project"],
        },
    ),
    Tool(
        name="codememory_health",
        description=(
            "Check backend connectivity and stats. Returns Ollama, Qdrant, "
            "FalkorDB status plus collection counts, metric summaries, and "
            "last ingest timestamp."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": _project_schema(),
            },
            "required": ["project"],
        },
    ),
    Tool(
        name="codememory_record_read",
        description="Record a filesystem read after an MCP tool call for efficiency tracking.",
        inputSchema={
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "Filesystem tool name (grep, read, bash, glob)"},
                "path": {"type": "string", "description": "File path or pattern accessed"},
                "session_id": {"type": "string", "description": "Session ID for correlation"},
                "project": _project_schema(),
                "chars": {"type": "integer", "description": "Output character count (optional)"},
            },
            "required": ["tool", "project"],
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


def _ingest(
    args: dict[str, Any],
    *,
    on_progress: Callable[[int, int | None, str], None] | None = None,
) -> list[TextContent]:
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

    # Default OFF: MCP ingest blocks transport and the host cannot show
    # progress mid-call. Steer the agent to the Bash CLI where progress
    # lines stream to stderr and `run_in_background` + `BashOutput` lets
    # the agent narrate progress turn-by-turn. Operators can re-enable
    # the in-MCP path via env var.
    mcp_path_enabled = os.environ.get("CODE_MEMORY_MCP_INGEST_ENABLED", "0") == "1"
    if not mcp_path_enabled:
        slug_arg = f" --project {project}" if project else ""
        full_arg = " --full" if full else ""
        since_arg = f" --since {since}" if since else ""
        dry_arg = " --dry-run" if dry_run else ""
        cmd = (
            f"code-memory ingest {root.resolve()}"
            f"{slug_arg}{full_arg}{since_arg}{dry_arg}"
        )
        return _text(
            {
                "status": "disabled_use_cli",
                "project": project,
                "root": str(root.resolve()),
                "mode": mode,
                "reason": (
                    "MCP ingest is disabled because the transport blocks for "
                    "the full duration of the call and the host does not "
                    "surface `notifications/progress` mid-call. The agent "
                    "would see only the final result and the user would see "
                    "no progress feedback."
                ),
                "run_this_instead": cmd,
                "agent_guidance": [
                    "Invoke the command above with the Bash tool.",
                    "Pass `run_in_background=true` so the call returns "
                    "immediately with a shell id.",
                    "Between turns, call `BashOutput(shell_id)` to read new "
                    "stderr lines and narrate progress to the user.",
                    "On completion, the final stdout payload is the same "
                    "JSON shape this MCP tool would have returned.",
                ],
                "human_guidance": (
                    "Run `tail -f` on the same process stderr (or pipe it to "
                    "a file) for a true live view independent of the agent."
                ),
                "override": (
                    "Set CODE_MEMORY_MCP_INGEST_ENABLED=1 in the MCP server "
                    "env to re-enable the in-MCP ingest path; then pass "
                    "`confirmed=true` on the tool call."
                ),
            }
        )

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
    stats = pipe.ingest_repo(
        root, mode=mode, since=since, dry_run=dry_run, on_progress=on_progress
    )
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


def _injects(args: dict[str, Any]) -> list[TextContent]:
    g, slug = _graph_for(_require_project(args))
    rows = g.injects(args["symbol"])
    return _text({"project": slug, "symbol": args["symbol"], "injects": rows})


def _injectors(args: dict[str, Any]) -> list[TextContent]:
    g, slug = _graph_for(_require_project(args))
    rows = g.injectors(args["token"])
    return _text({"project": slug, "token": args["token"], "injectors": rows})


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


def _drift(args: dict[str, Any]) -> list[TextContent]:
    g, slug = _graph_for(_require_project(args))
    head = args.get("head_sha")
    if not isinstance(head, str) or not head:
        return _text({"error": "ValueError", "message": "head_sha is required"})
    rows = g.drift(head)
    return _text(
        {"project": slug, "head_sha": head, "count": len(rows), "items": rows}
    )


def _compute_sha_ord(sha: str) -> int:
    """Compute topological ordinal for a git sha."""
    import subprocess

    repo = Path(os.environ.get("CODE_MEMORY_REPO") or os.getcwd()).resolve()
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "--first-parent", sha],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise ValueError(f"Cannot compute ordinal for sha {sha[:12]}: {out.stderr.strip()}")
    return int(out.stdout.strip())


def _at_sha(args: dict[str, Any]) -> list[TextContent]:
    g, slug = _graph_for(_require_project(args))
    sha = args.get("sha")
    sha_ord = args.get("sha_ord")
    if not isinstance(sha, str) or not sha:
        return _text({"error": "ValueError", "message": "sha is required"})
    if not isinstance(sha_ord, int):
        try:
            sha_ord = _compute_sha_ord(sha)
        except ValueError as e:
            return _text({"error": "ValueError", "message": str(e)})
    label = args.get("label", "Symbol")
    if label not in {"Symbol", "File"}:
        return _text(
            {"error": "ValueError", "message": "label must be 'Symbol' or 'File'"}
        )
    limit = int(args.get("limit", 200))
    rows = g.at_sha(sha, sha_ord, label=label, limit=limit)
    return _text(
        {
            "project": slug,
            "sha": sha,
            "sha_ord": sha_ord,
            "label": label,
            "count": len(rows),
            "items": rows,
        }
    )


def _callers_at_sha(args: dict[str, Any]) -> list[TextContent]:
    g, slug = _graph_for(_require_project(args))
    sha = args.get("sha")
    sha_ord = args.get("sha_ord")
    if not isinstance(sha, str) or not sha:
        return _text({"error": "ValueError", "message": "sha is required"})
    if not isinstance(sha_ord, int):
        try:
            sha_ord = _compute_sha_ord(sha)
        except ValueError as e:
            return _text({"error": "ValueError", "message": str(e)})
    rows = g.callers_at_sha(args["symbol"], sha, sha_ord)
    return _text(
        {
            "project": slug,
            "symbol": args["symbol"],
            "sha": sha,
            "sha_ord": sha_ord,
            "count": len(rows),
            "items": rows,
        }
    )


def _extract_claims(args: dict[str, Any]) -> list[TextContent]:
    """Run claim extraction over user prompts and persist results.

    Fire-and-forget contract from the caller's perspective: we never
    raise on a malformed prompt or a model glitch. Infra failures
    (Ollama unreachable) are returned as ``{"error": ...}`` so the
    hook can log and move on.
    """
    project = _require_project(args)
    if not CONFIG.claims_enabled:
        return _text(
            {
                "status": "disabled",
                "hint": "set CLAIMS_EXTRACTION=true (if disabled).",
            }
        )

    raw_prompts = args.get("prompts") or []
    if not isinstance(raw_prompts, list):
        return _text({"error": "ValueError", "message": "`prompts` must be a list."})

    normalized: list[tuple[str, float, str | None]] = []
    for item in raw_prompts:
        if isinstance(item, str):
            text = item.strip()
            if text:
                normalized.append((text, _now(), None))
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            ts = item.get("ts")
            ts_val = float(ts) if isinstance(ts, (int, float)) else _now()
            pid = item.get("id")
            pid_val = str(pid) if isinstance(pid, str) and pid else None
            normalized.append((text, ts_val, pid_val))

    if not normalized:
        return _text({"project": project, "claims_added": 0, "claims": []})

    session_id = args.get("session_id")
    session_val = str(session_id) if isinstance(session_id, str) and session_id else None

    repo = Path(os.environ.get("CODE_MEMORY_REPO") or os.getcwd()).resolve()
    head_sha = _head_sha_safe(repo)

    from .claims import (
        ClaimExtractor,
        ClaimRecord,
        ClaimsStore,
        EntityResolver,
    )
    from .claims.extractor import ExtractionError

    cfg = CONFIG.for_project(project)
    store = ClaimsStore(path=cfg.claims_db)
    extractor = ClaimExtractor()
    resolver: EntityResolver | None
    try:
        resolver = EntityResolver(project=project, cfg=cfg)
    except Exception:  # noqa: BLE001
        resolver = None
    added = 0
    samples: list[dict[str, Any]] = []
    try:
        for text, ts, pid in normalized:
            try:
                claims = extractor.extract(text)
            except ExtractionError as exc:
                return _text(
                    {
                        "project": project,
                        "error": "ExtractionError",
                        "message": str(exc),
                        "claims_added": added,
                    }
                )
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
                    valid_at=ts,
                    head_sha=head_sha,
                    session_id=session_val,
                    source_prompt_id=pid,
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

    return _text(
        {
            "project": project,
            "claims_added": added,
            "sample": samples,
        }
    )


def _assert_claim(args: dict[str, Any]) -> list[TextContent]:
    """Agent-authored claim. No LLM in the loop.

    Bypasses the ``claims_enabled`` (CLAIMS_EXTRACTION) flag because no
    Ollama call is made — the agent supplies the triple directly. The
    flag still gates the extractor path (``_extract_claims``).
    """
    project = _require_project(args)

    subject = args.get("subject")
    predicate = args.get("predicate")
    obj = args.get("object")
    for field_name, value in (
        ("subject", subject),
        ("predicate", predicate),
        ("object", obj),
    ):
        if not isinstance(value, str) or not value.strip():
            return _text(
                {
                    "error": "ValueError",
                    "message": f"`{field_name}` is required (non-empty string).",
                }
            )

    polarity = args.get("polarity", True)
    if not isinstance(polarity, bool):
        return _text(
            {"error": "ValueError", "message": "`polarity` must be a boolean."}
        )

    confidence = args.get("confidence", 0.95)
    try:
        confidence_val = float(confidence)
    except (TypeError, ValueError):
        return _text(
            {"error": "ValueError", "message": "`confidence` must be a number."}
        )
    if not 0.0 <= confidence_val <= 1.0:
        return _text(
            {
                "error": "ValueError",
                "message": "`confidence` must be in [0, 1].",
            }
        )

    evidence_raw = args.get("evidence_span")
    evidence_span = (
        evidence_raw.strip()
        if isinstance(evidence_raw, str) and evidence_raw.strip()
        else ""
    )

    valid_at_raw = args.get("valid_at")
    valid_at = (
        float(valid_at_raw)
        if isinstance(valid_at_raw, (int, float))
        else _now()
    )

    session_raw = args.get("session_id")
    session_id = (
        str(session_raw)
        if isinstance(session_raw, str) and session_raw
        else None
    )
    pid_raw = args.get("source_prompt_id")
    source_prompt_id = (
        str(pid_raw) if isinstance(pid_raw, str) and pid_raw else None
    )

    repo = Path(os.environ.get("CODE_MEMORY_REPO") or os.getcwd()).resolve()
    head_sha = _head_sha_safe(repo)

    from .claims import ClaimRecord, ClaimsStore, EntityResolver

    cfg = CONFIG.for_project(project)
    resolver: EntityResolver | None
    try:
        resolver = EntityResolver(project=project, cfg=cfg)
    except Exception:  # noqa: BLE001
        resolver = None

    subj_id = _resolve_or_none(resolver, subject)
    obj_id = _resolve_or_none(resolver, obj)

    # Predicate canonicalization mirrors the extractor: lowercase
    # kebab-case so single-valued contradiction handling works.
    canonical_pred = predicate.strip().lower().replace(" ", "-")

    rec = ClaimRecord(
        subject=subject.strip(),
        predicate=canonical_pred,
        object=obj.strip(),
        polarity=polarity,
        confidence=confidence_val,
        evidence_span=evidence_span,
        valid_at=valid_at,
        head_sha=head_sha,
        session_id=session_id,
        source_prompt_id=source_prompt_id,
        entity_subject_id=subj_id,
        entity_object_id=obj_id,
    )

    store = ClaimsStore(path=cfg.claims_db)
    try:
        claim_id = store.upsert(rec)
    finally:
        store.close()

    return _text(
        {
            "project": project,
            "claim_id": claim_id,
            "subject": rec.subject,
            "predicate": rec.predicate,
            "object": rec.object,
            "polarity": rec.polarity,
            "confidence": rec.confidence,
            "valid_at": rec.valid_at,
        }
    )


def _read_claims(args: dict[str, Any]) -> list[TextContent]:
    project = _require_project(args)
    from .claims import ClaimsStore

    cfg = CONFIG.for_project(project)
    store = ClaimsStore(path=cfg.claims_db)
    try:
        subject = args.get("subject")
        subject_val = str(subject) if isinstance(subject, str) and subject else None
        as_of = args.get("as_of")
        rows = (
            store.as_of(float(as_of), subject=subject_val)
            if isinstance(as_of, (int, float))
            else store.current(subject=subject_val)
        )
        limit = int(args.get("limit", 50))
        rows = rows[:limit]
    finally:
        store.close()

    return _text(
        {
            "project": project,
            "count": len(rows),
            "claims": [
                {
                    "subject": r.subject,
                    "predicate": r.predicate,
                    "object": r.object,
                    "polarity": r.polarity,
                    "confidence": r.confidence,
                    "valid_at": r.valid_at,
                    "valid_to": r.valid_to,
                    "head_sha": r.head_sha,
                }
                for r in rows
            ],
        }
    )


def _health(args: dict[str, Any]) -> list[TextContent]:
    """Health check across all backends and storage."""
    import time as _time
    import httpx as _httpx

    project = _require_project(args)
    cfg = CONFIG.for_project(project)

    results: dict[str, Any] = {"project": project, "backends": {}}

    # Ollama
    t0 = _time.time()
    try:
        with _httpx.Client(timeout=5) as c:
            r = c.get(f"{CONFIG.ollama_url}/api/tags")
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            results["backends"]["ollama"] = {
                "status": "ok",
                "url": CONFIG.ollama_url,
                "latency_ms": round((_time.time() - t0) * 1000),
                "models": models,
            }
    except Exception as exc:
        results["backends"]["ollama"] = {
            "status": "error",
            "url": CONFIG.ollama_url,
            "error": str(exc),
        }

    # Qdrant
    t0 = _time.time()
    try:
        from .vector import QdrantStore

        q = QdrantStore()
        info = q.client.get_collection(cfg.qdrant_code)
        results["backends"]["qdrant"] = {
            "status": "ok",
            "url": CONFIG.qdrant_url,
            "latency_ms": round((_time.time() - t0) * 1000),
            "collections": {
                "code": {
                    "name": cfg.qdrant_code,
                    "vectors": info.points_count,
                },
            },
        }
    except Exception as exc:
        results["backends"]["qdrant"] = {
            "status": "error",
            "url": CONFIG.qdrant_url,
            "error": str(exc),
        }

    # FalkorDB
    t0 = _time.time()
    try:
        from .graph.falkor_store import FalkorStore

        g = FalkorStore(graph_name=cfg.falkor_graph)
        node_count = int(g.graph.query("MATCH (n) RETURN count(n)").result_set[0][0])
        results["backends"]["falkordb"] = {
            "status": "ok",
            "host": CONFIG.falkor_host,
            "port": CONFIG.falkor_port,
            "latency_ms": round((_time.time() - t0) * 1000),
            "graph": cfg.falkor_graph,
            "nodes": node_count,
        }
    except Exception as exc:
        results["backends"]["falkordb"] = {
            "status": "error",
            "host": CONFIG.falkor_host,
            "port": CONFIG.falkor_port,
            "error": str(exc),
        }

    # Storage stats
    results["storage"] = {
        "data_dir": str(cfg.data_dir),
    }

    # Episodic count
    try:
        from .episodic import EpisodicStore

        eps = EpisodicStore(path=cfg.episodic_db)
        row = eps.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()
        results["storage"]["episodes"] = {
            "path": str(cfg.episodic_db),
            "exists": cfg.episodic_db.exists(),
            "count": row[0] if row else 0,
        }
    except Exception:
        pass

    # Claims count
    try:
        from .claims import ClaimsStore

        cs = ClaimsStore(path=cfg.claims_db)
        results["storage"]["claims"] = {
            "path": str(cfg.claims_db),
            "exists": cfg.claims_db.exists(),
            "count": len(cs.current()) if cfg.claims_db.exists() else 0,
        }
        cs.close()
    except Exception:
        pass

    # Metrics summary. Always emit the block so callers can tell
    # "no data yet" apart from "metrics module disabled or broken".
    env_metrics = os.environ.get("CODEMEMORY_METRICS_DB")
    metrics_path = Path(env_metrics) if env_metrics else cfg.data_dir / "metrics.db"
    metrics_block: dict[str, Any] = {
        "path": str(metrics_path),
        "exists": metrics_path.exists(),
    }
    if metrics_block["exists"]:
        try:
            from .metrics import MetricsStore

            ms = MetricsStore(metrics_path)
            metrics_block.update(ms.summary())
            metrics_block["tool_usage"] = ms.tool_usage_summary()
            metrics_block["efficiency"] = ms.efficiency_summary()
        except Exception as exc:
            metrics_block["error"] = f"{type(exc).__name__}: {exc}"
    results["metrics"] = metrics_block

    # Last ingest SHA
    try:
        repo = Path(
            os.environ.get("CODE_MEMORY_REPO") or os.getcwd()
        ).resolve()
        if (repo / ".git").exists():
            from .orchestrator.ingest_state import IngestStateStore

            # IngestState lives alongside episodes in the same DB
            epdb = cfg.episodic_db
            if epdb.exists():
                st = IngestStateStore(epdb)
                state = st.get(repo)
                if state is not None:
                    results["last_ingest"] = {
                        "sha": state.last_sha,
                        "ts": state.last_ts,
                    }
    except Exception:
        pass

    return _text(results)


def _record_read(args: dict[str, Any]) -> list[TextContent]:
    project = _require_project(args)
    tool = args.get("tool", "")
    path = args.get("path", "")
    chars = int(args.get("chars", 0) or 0)
    session_id = str(args.get("session_id") or "")
    db_path = os.environ.get("CODEMEMORY_METRICS_DB") or str(CONFIG.data_dir / "metrics.db")
    try:
        from .metrics import MetricsStore
        ms = MetricsStore(Path(db_path))
        ms.record_fs_read(tool=tool, path=path, project=project, output_chars=chars, session_id=session_id)
        return _text({"recorded": True})
    except Exception as exc:
        return _text({"recorded": False, "error": str(exc)})


def _now() -> float:
    import time

    return time.time()


def _resolve_or_none(resolver: Any, text: str) -> str | None:
    """Defensive entity resolution helper (see CLI counterpart)."""
    if resolver is None:
        return None
    try:
        ref = resolver.resolve(text)
    except Exception:  # noqa: BLE001
        return None
    return ref.id if ref is not None else None


def _head_sha_safe(repo: Path) -> str | None:
    if not (repo / ".git").exists():
        return None
    try:
        from .orchestrator import git_delta

        return git_delta.head_sha(repo)
    except Exception:  # noqa: BLE001
        return None


def _record_tool_call_if_configured(tool: str, args: dict, output_chars: int) -> None:
    """Record tool call to MetricsStore if configured. Fire-and-forget."""
    try:
        db_path = os.environ.get("CODEMEMORY_METRICS_DB") or str(CONFIG.data_dir / "metrics.db")
        from .metrics import MetricsStore
        ms = MetricsStore(Path(db_path))
        query_text = str(args.get("query") or args.get("symbol") or args.get("target") or args.get("prompt") or "")
        result_count = _extract_result_count(tool, args, output_chars)
        ms.record_tool_call(
            tool=tool,
            project=args.get("project", ""),
            query_text=query_text[:500],
            output_chars=output_chars,
            result_count=result_count,
            session_id=str(args.get("session_id") or ""),
        )
    except Exception:
        pass


def _extract_result_count(tool: str, args: dict, output_chars: int) -> int:
    """Estimate result count from context. Best-effort."""
    k = int(args.get("k", 0) or 0)
    eps = int(args.get("eps", 0) or 0)
    if k or eps:
        return k + eps
    return 0


_HANDLERS = {
    "codememory_retrieve": _retrieve,
    "codememory_record": _record,
    "codememory_reingest": _reingest,
    "codememory_ingest": _ingest,
    "codememory_callers": _callers,
    "codememory_callees": _callees,
    "codememory_importers": _importers,
    "codememory_dependencies": _dependencies,
    "codememory_injects": _injects,
    "codememory_injectors": _injectors,
    "codememory_definitions": _definitions,
    "codememory_assembly_members": _assembly_members,
    "codememory_drift": _drift,
    "codememory_at_sha": _at_sha,
    "codememory_callers_at_sha": _callers_at_sha,
    "codememory_extract_claims": _extract_claims,
    "codememory_assert_claim": _assert_claim,
    "codememory_claims": _read_claims,
    "codememory_health": _health,
    "codememory_record_read": _record_read,
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

        # Bridge MCP `notifications/progress` for long-running tools. The
        # client opts in by sending `_meta.progressToken` on the request;
        # the SDK exposes it as `request_context.meta.progressToken`. We
        # hand the ingest pipeline a *sync* callback that schedules the
        # async send back onto this event loop via `from_thread.run`.
        progress_token: str | int | None = None
        try:
            ctx = server.request_context
            if ctx.meta is not None:
                progress_token = ctx.meta.progressToken
        except LookupError:
            ctx = None  # type: ignore[assignment]

        on_progress: Callable[[int, int | None, str], None] | None = None
        if name == "codememory_ingest":
            # Log token state on every ingest invocation so the user can
            # tell from MCP server logs whether the host (Claude Code,
            # OpenCode, ...) opted into progress at all. Without a token
            # the spec forbids sending; the call runs silently end-to-end.
            if progress_token is None:
                log.info(
                    "ingest: no progressToken on request — client did "
                    "not opt into notifications/progress"
                )
            else:
                log.info("ingest: progressToken=%r — streaming progress", progress_token)
        if progress_token is not None and ctx is not None:
            session = ctx.session
            token = progress_token
            request_id = str(ctx.request_id) if ctx.request_id is not None else None
            send_count = {"n": 0}

            async def _emit(
                completed: float, total: float | None, message: str
            ) -> None:
                await session.send_progress_notification(
                    token,
                    completed,
                    total,
                    message,
                    related_request_id=request_id,
                )

            def _send(completed: int, total: int | None, message: str) -> None:
                try:
                    from anyio.from_thread import run as _run_in_loop

                    _run_in_loop(
                        _emit,
                        float(completed),
                        float(total) if total is not None else None,
                        message,
                    )
                    send_count["n"] += 1
                    if send_count["n"] == 1 or send_count["n"] % 25 == 0:
                        log.info(
                            "progress sent #%d: %s", send_count["n"], message
                        )
                except Exception as exc:  # noqa: BLE001 — UI errors must
                    # never abort the ingest worker thread.
                    log.warning("progress notification failed: %s", exc)

            on_progress = _send

        def _invoke() -> list[TextContent]:
            if name == "codememory_ingest":
                return _ingest(arguments, on_progress=on_progress)
            return handler(arguments)

        try:
            result = await anyio.to_thread.run_sync(_invoke)
        except Exception as exc:  # surface, don't crash the server
            return _text({"error": type(exc).__name__, "message": str(exc)})
        # Auto-record MCP tool call for efficiency tracking (fire-and-forget)
        output_chars = sum(len(t.text) for t in result if hasattr(t, "text"))
        _record_tool_call_if_configured(name, arguments, output_chars)
        return result

    return server


def _bootstrap_repo() -> Path | None:
    """Locate the active repo and ensure autostart + in-process watcher.

    Best-effort: any failure (no git, no write permission, missing
    watchdog dep) logs and continues. The MCP server still serves
    queries even if these side-channels can't be set up.
    """
    # 0. Backend health check (best-effort)
    if not os.environ.get("CODE_MEMORY_NO_HEALTH_CHECK"):
        _check_backends()

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


def _check_backends() -> None:
    """Ping backends at startup. Logs errors but never crashes."""
    import httpx as _httpx

    # Ollama
    try:
        with _httpx.Client(timeout=5) as c:
            r = c.get(f"{CONFIG.ollama_url}/api/tags")
            r.raise_for_status()
            log.info("health: ollama ok (%s)", CONFIG.ollama_url)
    except Exception as exc:
        log.error("health: ollama UNREACHABLE (%s): %s", CONFIG.ollama_url, exc)
    # Qdrant
    try:
        with _httpx.Client(timeout=3) as c:
            r = c.get(f"{CONFIG.qdrant_url}/healthz")
            r.raise_for_status()
            log.info("health: qdrant ok (%s)", CONFIG.qdrant_url)
    except Exception as exc:
        log.error("health: qdrant UNREACHABLE (%s): %s", CONFIG.qdrant_url, exc)
    # FalkorDB
    try:
        import redis as _redis

        r = _redis.Redis(
            host=CONFIG.falkor_host,
            port=CONFIG.falkor_port,
            socket_timeout=3,
        )
        r.ping()
        log.info(
            "health: falkordb ok (%s:%d)",
            CONFIG.falkor_host,
            CONFIG.falkor_port,
        )
    except Exception as exc:
        log.error(
            "health: falkordb UNREACHABLE (%s:%d): %s",
            CONFIG.falkor_host,
            CONFIG.falkor_port,
            exc,
        )


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
