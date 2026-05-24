from __future__ import annotations

import hashlib
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..config import CONFIG, Config, detect_project_slug
from ..embed import M3Embedder, get_embedder
from ..episodic import Episode, EpisodicStore
from ..episodic.sqlite_store import episode_payload, episode_text
from ..extractor import ExtractedFile, Extractor, Symbol
from ..extractor.csproj import CsprojInfo, walk_csprojs
from ..extractor.sanity import SUSPECT_THRESHOLD, SanitySummary
from ..graph import FalkorStore, GraphEdge, GraphNode
from ..vector import QdrantStore, VectorRecord
from . import git_delta
from .ingest_state import IngestStateStore
from .resolver import resolve_graph

IngestMode = Literal["auto", "full", "incremental"]


def _id(*parts: str) -> str:
    h = hashlib.sha1("\x00".join(parts).encode()).hexdigest()
    return h[:32]


# How often to emit a progress heartbeat during ingest. Heartbeats go to
# stderr so ``--json`` output on stdout stays clean.
_PROGRESS_EVERY = int(os.environ.get("CODEMEMORY_PROGRESS_EVERY", "50"))
_PROGRESS_ENABLED = os.environ.get("CODEMEMORY_PROGRESS", "1") != "0"


class _Heartbeat:
    """Emit periodic ``files=… symbols=…`` lines to stderr during ingest."""

    def __init__(self, label: str, *, total: int | None = None) -> None:
        self.label = label
        self.total = total
        self.start = time.monotonic()
        self.last = self.start

    def tick(self, stats: IngestStats) -> None:
        if not _PROGRESS_ENABLED:
            return
        if _PROGRESS_EVERY <= 0:
            return
        if stats.files % _PROGRESS_EVERY != 0 or stats.files == 0:
            return
        now = time.monotonic()
        elapsed = max(now - self.start, 1e-6)
        rate = stats.files / elapsed
        eta = ""
        if self.total and rate > 0:
            remaining = max(self.total - stats.files, 0)
            eta = f" eta={remaining / rate:.0f}s"
        total_part = f"/{self.total}" if self.total else ""
        sys.stderr.write(
            f"[code-memory] {self.label}: files={stats.files}{total_part} "
            f"symbols={stats.symbols} chunks={stats.chunks} "
            f"skipped={stats.skipped} rate={rate:.1f}/s{eta}\n"
        )
        sys.stderr.flush()
        self.last = now

    def done(self, stats: IngestStats) -> None:
        if not _PROGRESS_ENABLED:
            return
        elapsed = time.monotonic() - self.start
        sys.stderr.write(
            f"[code-memory] {self.label} done: files={stats.files} "
            f"symbols={stats.symbols} chunks={stats.chunks} "
            f"skipped={stats.skipped} elapsed={elapsed:.1f}s\n"
        )
        sys.stderr.flush()


@dataclass
class IngestStats:
    files: int = 0
    symbols: int = 0
    imports: int = 0
    calls: int = 0
    chunks: int = 0
    deleted: int = 0
    skipped: int = 0
    mode: str = "full"
    base_sha: str | None = None
    head_sha: str | None = None
    resolver: dict[str, int] | None = None
    sanity: dict[str, object] | None = None
    projects: dict[str, int] | None = None
    notes: list[str] = field(default_factory=list)


class Pipeline:
    """Coordinator: extractor -> graph + vectors + episodes."""

    def __init__(
        self,
        project: str | None = None,
        embedder: M3Embedder | None = None,
        vector: QdrantStore | None = None,
        graph: FalkorStore | None = None,
        episodic: EpisodicStore | None = None,
    ) -> None:
        self.slug = project or detect_project_slug()
        self.cfg: Config = CONFIG.for_project(self.slug)
        self.embedder = embedder or get_embedder()
        self.vector = vector or QdrantStore()
        self.graph = graph or FalkorStore(graph_name=self.cfg.falkor_graph)
        self.episodic = episodic or EpisodicStore(path=self.cfg.episodic_db)
        self.vector.ensure_collection(self.cfg.qdrant_code)
        self.vector.ensure_collection(self.cfg.qdrant_episodes)
        self.graph.ensure_indexes()
        self.state = IngestStateStore(self.cfg.episodic_db)

    def ingest_repo(
        self,
        root: str | Path,
        *,
        mode: IngestMode = "auto",
        since: str | None = None,
        dry_run: bool = False,
    ) -> IngestStats:
        """Ingest a repository.

        mode:
          - "auto": git-incremental if prior state exists and base is reachable,
                   else full walk
          - "full": purge this project's vectors+graph+ingest_state, then
                    walk every file. Use to rebuild from scratch.
          - "incremental": require git + base; raise if not available
        since: explicit base ref (branch/tag/sha). Overrides stored state when set.
        dry_run: compute plan and return stats with notes; don't touch storage.
        """
        root_path = Path(root).resolve()
        is_git = git_delta.is_git_repo(root_path)

        if mode == "full" or (mode == "auto" and not is_git):
            stats = self._ingest_full(root_path, dry_run=dry_run)
            if not dry_run:
                self._run_resolver(stats)
            if is_git and not dry_run:
                self._record_state(root_path, stats)
            return stats

        # git path
        if not is_git:
            raise RuntimeError(f"{root_path} is not a git repository (mode={mode!r})")

        head = git_delta.head_sha(root_path)
        branch = git_delta.current_branch(root_path)
        base = self._resolve_base(root_path, since=since, mode=mode)

        if base is None:
            # auto + git + no prior + no --since => full walk, then record state
            stats = self._ingest_full(root_path, dry_run=dry_run)
            stats.head_sha = head
            stats.notes.append("no prior ingest state; performed full walk")
            if not dry_run:
                self._run_resolver(stats)
                self._record_state(root_path, stats, head=head, branch=branch)
            return stats

        # Incremental
        delta = git_delta.changed_since(root_path, base, include_dirty=True)
        stats = self._ingest_delta(
            root_path, delta, base_sha=base, head_sha=head, dry_run=dry_run
        )
        stats.mode = "incremental"
        if not dry_run:
            if stats.files > 0:
                # Only run resolver if something actually changed; the
                # resolver scans the whole graph so it's a fixed cost
                # we'd rather skip on no-op delta runs.
                self._run_resolver(stats)
            self._record_state(root_path, stats, head=head, branch=branch)
        return stats

    # -- internals -------------------------------------------------------

    def _resolve_base(
        self, root: Path, *, since: str | None, mode: IngestMode
    ) -> str | None:
        if since is not None:
            try:
                return git_delta.resolve_ref(root, since)
            except git_delta.GitError as e:
                raise RuntimeError(f"could not resolve --since {since!r}: {e}") from e

        prior = self.state.get(root)
        if prior is None:
            if mode == "incremental":
                raise RuntimeError(
                    f"no prior ingest state for {root}; run a full ingest first"
                )
            return None

        if not git_delta.is_reachable(root, prior.last_sha):
            # history rewrite or branch deletion — fall back
            self.state.clear(root)
            return None

        return prior.last_sha

    def _ingest_full(self, root: Path, *, dry_run: bool) -> IngestStats:
        extractor = Extractor()
        stats = IngestStats(mode="full")
        sanity = SanitySummary()
        if not dry_run:
            self._purge_project_index(root)
        hb = _Heartbeat("full ingest" + (" (dry-run)" if dry_run else ""))
        for ex in extractor.walk(root):
            stats.files += 1
            stats.symbols += len(ex.symbols)
            stats.imports += len(ex.imports)
            stats.calls += len(ex.calls)
            stats.chunks += len(ex.symbols) or 1
            sanity.record(ex)
            if not dry_run:
                self.ingest_file(ex)
            hb.tick(stats)
        hb.done(stats)
        _attach_sanity(stats, sanity)
        self._ingest_dotnet_projects(root, stats, dry_run=dry_run)
        return stats

    def _run_resolver(self, stats: IngestStats) -> None:
        """Resolve placeholder ``name::X`` Symbol nodes to real symbols.

        Records resolver stats on the ingest stats object so callers can
        see how much of the call graph is now grounded vs. ambiguous.
        Failures are non-fatal — ingest data is already persisted.
        """
        try:
            r = resolve_graph(self.graph)
        except Exception as e:
            stats.notes.append(f"resolver skipped: {e}")
            return
        stats.resolver = {
            "placeholders": r.placeholders,
            "edges_total": r.edges_total,
            "resolved_same_file": r.edges_resolved_same_file,
            "resolved_imported": r.edges_resolved_imported,
            "resolved_unique": r.edges_resolved_unique,
            "ambiguous": r.edges_left_ambiguous,
            "external": r.edges_left_external,
            "placeholders_deleted": r.placeholders_deleted,
        }

    def _ingest_dotnet_projects(
        self, root: Path, stats: IngestStats, *, dry_run: bool
    ) -> None:
        """Walk `.csproj`/`.fsproj`/`.vbproj` and emit Project topology.

        Adds three node/edge kinds to the graph:

        * ``Project`` nodes keyed by absolute path.
        * ``PROJECT_REFERENCES`` edges (Project → Project) from every
          ``<ProjectReference>``. Targets outside the repo or unparseable
          are silently dropped — see ``parse_csproj``.
        * ``PACKAGE_REFERENCES`` edges (Project → Package) from every
          ``<PackageReference>``. ``Package`` is a new label so NuGet
          packages don't pollute the ``Module`` namespace (which holds
          `using` import targets).

        Non-.NET repos see zero ``.csproj`` files and this is a no-op.
        Failures are non-fatal: source ingest already happened.
        """
        try:
            projects = walk_csprojs(root)
        except Exception as e:  # noqa: BLE001
            stats.notes.append(f"csproj indexing skipped: {e}")
            return
        if not projects:
            return
        counts = {
            "projects": len(projects),
            "project_refs": sum(len(p.project_references) for p in projects),
            "package_refs": sum(len(p.package_references) for p in projects),
        }
        stats.projects = counts
        if dry_run:
            return
        self._upsert_dotnet_projects(projects)

    def _upsert_dotnet_projects(self, projects: list[CsprojInfo]) -> None:
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        seen_pkgs: set[str] = set()
        for proj in projects:
            props: dict[str, object] = {
                "name": proj.name,
                "assembly_name": proj.assembly_name or proj.name,
                "sdk_style": proj.sdk_style,
            }
            if proj.target_framework:
                props["target_framework"] = proj.target_framework
            nodes.append(GraphNode(label="Project", key=proj.path, props=props))
            for ref in proj.project_references:
                # Forward-reference target Project node — `upsert_nodes`
                # is idempotent, and walking all projects first then
                # writing edges would require two passes for no win.
                nodes.append(GraphNode(label="Project", key=ref))
                edges.append(
                    GraphEdge(
                        type="PROJECT_REFERENCES",
                        src_label="Project",
                        src_key=proj.path,
                        dst_label="Project",
                        dst_key=ref,
                    )
                )
            for pkg in proj.package_references:
                key = pkg.name
                if key not in seen_pkgs:
                    seen_pkgs.add(key)
                    nodes.append(
                        GraphNode(
                            label="Package",
                            key=key,
                            props={"name": pkg.name},
                        )
                    )
                edge_props: dict[str, object] = {}
                if pkg.version:
                    edge_props["version"] = pkg.version
                edges.append(
                    GraphEdge(
                        type="PACKAGE_REFERENCES",
                        src_label="Project",
                        src_key=proj.path,
                        dst_label="Package",
                        dst_key=key,
                        props=edge_props,
                    )
                )
        self.graph.upsert_nodes(nodes)
        self.graph.upsert_edges(edges)

    def _purge_project_index(self, root: Path) -> None:
        """Wipe code vectors + graph + ingest_state for this project.

        Episodes are independent (conversation memory) and preserved.
        Called before a full re-ingest so stale entries (e.g. paths now
        excluded by .gitignore or ignore_dirs) don't linger in retrieval.
        """
        self.vector.recreate_collection(self.cfg.qdrant_code)
        self.graph.clear_graph()
        self.state.clear(root)

    def _ingest_delta(
        self,
        root: Path,
        delta: git_delta.Delta,
        *,
        base_sha: str,
        head_sha: str,
        dry_run: bool,
    ) -> IngestStats:
        stats = IngestStats(mode="incremental", base_sha=base_sha, head_sha=head_sha)
        sanity = SanitySummary()
        reingest = list(delta.reingest_paths())
        hb = _Heartbeat(
            "incremental ingest" + (" (dry-run)" if dry_run else ""),
            total=len(reingest),
        )

        for path in delta.deleted:
            path_str = str(path)
            stats.deleted += 1
            if dry_run:
                continue
            self.graph.delete_file(path_str)
            self.vector.delete_by_path(self.cfg.qdrant_code, path_str)

        for path in reingest:
            if not path.is_file():
                # file deleted between diff and now, or extractor can't see it
                stats.skipped += 1
                continue
            if dry_run:
                ex = self._extract_one(path)
                if ex is None:
                    stats.skipped += 1
                    continue
                stats.files += 1
                stats.symbols += len(ex.symbols)
                stats.imports += len(ex.imports)
                stats.calls += len(ex.calls)
                stats.chunks += len(ex.symbols) or 1
                sanity.record(ex)
                continue

            ex = self.reingest_file(path)
            if ex is None:
                stats.skipped += 1
                continue
            stats.files += 1
            stats.symbols += len(ex.symbols)
            stats.imports += len(ex.imports)
            stats.calls += len(ex.calls)
            stats.chunks += len(ex.symbols) or 1
            sanity.record(ex)
            hb.tick(stats)

        hb.done(stats)
        _attach_sanity(stats, sanity)
        # Re-run csproj indexing on every delta — project files are
        # tiny and the topology shifts independently of source edits.
        self._ingest_dotnet_projects(root, stats, dry_run=dry_run)
        if delta.is_empty:
            stats.notes.append("no changes since last ingest")
        return stats

    @staticmethod
    def _extract_one(path: Path) -> ExtractedFile | None:
        from ..extractor.treesitter import extract_file

        return extract_file(path)

    def _record_state(
        self,
        root: Path,
        stats: IngestStats,
        *,
        head: str | None = None,
        branch: str | None = None,
    ) -> None:
        sha = head or stats.head_sha
        if sha is None and git_delta.is_git_repo(root):
            try:
                sha = git_delta.head_sha(root)
                if branch is None:
                    branch = git_delta.current_branch(root)
            except git_delta.GitError:
                sha = None
        if sha is None:
            return
        stats.head_sha = sha
        self.state.set(root, sha=sha, branch=branch)

    def ingest_file(self, ex: ExtractedFile) -> None:
        self._upsert_graph(ex)
        self._upsert_vectors(ex)

    def reingest_file(self, path: str | Path) -> ExtractedFile | None:
        from ..extractor.treesitter import extract_file

        ex = extract_file(path)
        if ex is None:
            return None
        self.graph.delete_file(ex.path)
        self.vector.delete_by_path(self.cfg.qdrant_code, ex.path)
        self.ingest_file(ex)
        return ex

    def record_episode(self, ep: Episode) -> str:
        ep_id = self.episodic.add(ep)
        hv = self.embedder.embed_one(episode_text(ep))
        self.vector.upsert(
            self.cfg.qdrant_episodes,
            [VectorRecord(id=ep_id, vector=hv, payload=episode_payload(ep))],
        )
        return ep_id

    def _upsert_graph(self, ex: ExtractedFile) -> None:
        file_node = GraphNode(
            label="File",
            key=ex.path,
            props={"lang": ex.lang, "generated": ex.generated},
        )
        nodes: list[GraphNode] = [file_node]
        edges: list[GraphEdge] = []

        for s in ex.symbols:
            sym_key = _symbol_key(ex.path, s)
            props: dict[str, object] = {
                "name": s.name,
                "kind": s.kind,
                "start": s.start_line,
                "end": s.end_line,
                "file": ex.path,
            }
            if s.namespace:
                props["namespace"] = s.namespace
            if s.partial:
                # Partial declarations live in multiple files; the per-key
                # ``file`` / ``start`` / ``end`` reflect *one* part. The
                # ``partial`` flag tells consumers to expect siblings.
                props["partial"] = True
            nodes.append(GraphNode(label="Symbol", key=sym_key, props=props))
            edges.append(
                GraphEdge(
                    type="DEFINES",
                    src_label="File",
                    src_key=ex.path,
                    dst_label="Symbol",
                    dst_key=sym_key,
                )
            )

        seen_mods = set()
        for mod in ex.imports:
            if mod in seen_mods:
                continue
            seen_mods.add(mod)
            nodes.append(GraphNode(label="Module", key=mod))
            edges.append(
                GraphEdge(
                    type="IMPORTS",
                    src_label="File",
                    src_key=ex.path,
                    dst_label="Module",
                    dst_key=mod,
                )
            )

        seen_calls = set()
        for callee in ex.calls:
            if callee in seen_calls:
                continue
            seen_calls.add(callee)
            edges.append(
                GraphEdge(
                    type="CALLS",
                    src_label="File",
                    src_key=ex.path,
                    dst_label="Symbol",
                    dst_key=f"name::{callee}",
                    props={"unresolved": True},
                )
            )
            nodes.append(
                GraphNode(label="Symbol", key=f"name::{callee}", props={"name": callee, "unresolved": True})
            )

        self.graph.upsert_nodes(nodes)
        self.graph.upsert_edges(edges)

    def _upsert_vectors(self, ex: ExtractedFile, batch_size: int = 32) -> None:
        chunks = list(_chunks_for(ex))
        if not chunks:
            return
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            hvecs = self.embedder.embed([c.text for c in batch])
            records = [
                VectorRecord(
                    id=_id(ex.path, c.key),
                    vector=hv,
                    payload={
                        "path": ex.path,
                        "lang": ex.lang,
                        "kind": c.kind,
                        "name": c.name,
                        "start": c.start,
                        "end": c.end,
                        "generated": ex.generated,
                    },
                )
                for c, hv in zip(batch, hvecs, strict=True)
            ]
            self.vector.upsert(self.cfg.qdrant_code, records)


def _symbol_key(path: str, sym: Symbol) -> str:
    """Build the graph key for a Symbol node.

    Non-partial symbols stay file-scoped — ``{path}::{name}#{line}``.
    Partial declarations with a known namespace collapse to one key
    across every file that declares a part — ``partial::{ns}.{name}``.
    Multiple ``DEFINES`` edges from the contributing files all point
    at the same Symbol node, so callers/callees queries see one
    logical entity instead of N orphan duplicates.

    Partial declarations without a resolvable namespace are rare
    (global namespace, error recovery); fall back to file-scoped so
    we never collide two unrelated globals.
    """
    if sym.partial and sym.namespace:
        return f"partial::{sym.namespace}.{sym.name}"
    return f"{path}::{sym.name}#{sym.start_line}"


def _attach_sanity(stats: IngestStats, sanity: SanitySummary) -> None:
    """Record sanity-check results on ``stats`` and warn on high failure rates.

    A symbol fails the round-trip when its snippet doesn't contain its
    own (plain-identifier) name verbatim. That happens when the
    extractor's byte/char accounting is broken — historically the
    UTF-8 chop bug. Surface failures on the stats object so the CLI
    output shows them, and append a loud note when the rate crosses
    the suspect threshold so a human looks.
    """
    if sanity.symbols_checked == 0:
        return
    rate = sanity.failure_rate
    stats.sanity = {
        "checked": sanity.symbols_checked,
        "failed": sanity.symbols_failed,
        "failure_rate": round(rate, 4),
        "samples": [
            {"path": v.path, "name": v.name, "kind": v.kind, "line": v.start_line}
            for v in sanity.sample_violations
        ],
    }
    if rate > SUSPECT_THRESHOLD:
        stats.notes.append(
            f"sanity: {sanity.symbols_failed}/{sanity.symbols_checked} "
            f"plain-identifier symbols ({rate * 100:.1f}%) did not round-trip; "
            f"extractor may be miscounting offsets — see stats.sanity.samples"
        )


@dataclass
class _Chunk:
    key: str
    text: str
    kind: str
    name: str
    start: int
    end: int


def _chunks_for(ex: ExtractedFile) -> Iterable[_Chunk]:
    if ex.symbols:
        for s in ex.symbols:
            yield _Chunk(
                key=f"{s.name}#{s.start_line}",
                text=_symbol_text(s, ex.path),
                kind=s.kind,
                name=s.name,
                start=s.start_line,
                end=s.end_line,
            )
    else:
        # fallback: whole file (cap to ~6k chars)
        snippet = ex.source[:6000]
        yield _Chunk(
            key="file",
            text=f"FILE {ex.path}\n{snippet}",
            kind="file",
            name=Path(ex.path).name,
            start=1,
            end=len(ex.source.splitlines()) or 1,
        )


MAX_SNIPPET_CHARS = 1500
SIGNATURE_LINES = 3


def _symbol_text(s: Symbol, path: str) -> str:
    """Build chunk text optimised for hybrid (dense + sparse) embedding.

    Layout:
      1. Header line with file/kind/name/symbol — front-loaded so both
         dense semantics and sparse identifier weights pick it up.
      2. Signature lines (first ``SIGNATURE_LINES`` non-empty) — repeated
         so they survive aggressive tail-trim and dominate the lexical
         weighting for short queries like ``ngOnInit`` or
         ``UserService.create``.
      3. Body, tail-trimmed at ``MAX_SNIPPET_CHARS``. 1500 chars (~ 400
         tokens) keeps the m3 forward pass tight; longer bodies dilute
         dense quality without buying much.

    Empty / one-line symbols still produce a usable chunk because the
    header alone carries the identifier signal.
    """
    snippet = s.snippet or ""
    lines = [line for line in snippet.splitlines() if line.strip()]
    signature = "\n".join(lines[:SIGNATURE_LINES])
    body = snippet[:MAX_SNIPPET_CHARS]
    parts = [
        f"FILE {path}",
        f"KIND {s.kind} NAME {s.name}",
    ]
    if signature:
        parts.append(f"SIGNATURE\n{signature}")
    parts.append(body)
    return "\n".join(parts)
