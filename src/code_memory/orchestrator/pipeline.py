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
        if not dry_run:
            self._purge_project_index(root)
        hb = _Heartbeat("full ingest" + (" (dry-run)" if dry_run else ""))
        for ex in extractor.walk(root):
            stats.files += 1
            stats.symbols += len(ex.symbols)
            stats.imports += len(ex.imports)
            stats.calls += len(ex.calls)
            stats.chunks += len(ex.symbols) or 1
            if not dry_run:
                self.ingest_file(ex)
            hb.tick(stats)
        hb.done(stats)
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
            hb.tick(stats)

        hb.done(stats)
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
            sym_key = f"{ex.path}::{s.name}#{s.start_line}"
            nodes.append(
                GraphNode(
                    label="Symbol",
                    key=sym_key,
                    props={
                        "name": s.name,
                        "kind": s.kind,
                        "start": s.start_line,
                        "end": s.end_line,
                        "file": ex.path,
                    },
                )
            )
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
