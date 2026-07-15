from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..config import CONFIG, Config, detect_project_slug
from ..embed import M3Embedder, get_embedder
from ..episodic import Episode, EpisodicStore
from ..episodic.sqlite_store import episode_payload, episode_text
from ..extractor import ExtractedFile, Extractor, Symbol
from ..extractor.csproj import CsprojInfo, walk_csprojs
from ..extractor.dll import parse_assembly
from ..extractor.nuget import resolve_refs
from ..extractor.sanity import SUSPECT_THRESHOLD, SanitySummary
from ..extractor.sln import walk_solutions
from ..extractor.treesitter import DEFAULT_IGNORE_DIRS, LANG_BY_EXT
from ..graph import FalkorStore, GraphEdge, GraphNode
from ..vector import QdrantStore, VectorRecord
from . import git_delta
from .ingest_state import IngestState, IngestStateStore
from .resolver import resolve_graph

IngestMode = Literal["auto", "full", "incremental"]

ProgressCallback = Callable[[int, int | None, str], None]


def _id(*parts: str) -> str:
    h = hashlib.sha1("\x00".join(parts).encode()).hexdigest()
    return h[:32]


# How often to emit a progress heartbeat during ingest. Heartbeats go to
# stderr so ``--json`` output on stdout stays clean.
_PROGRESS_EVERY = int(os.environ.get("CODEMEMORY_PROGRESS_EVERY", "50"))
_PROGRESS_ENABLED = os.environ.get("CODEMEMORY_PROGRESS", "1") != "0"
# auto = rich TUI when stderr is a TTY, plain text otherwise.
# rich  = force rich (e.g. forced inside non-TTY harness that handles ANSI).
# text  = legacy throttled heartbeat lines.
# none  = silence everything.
_PROGRESS_STYLE = os.environ.get("CODEMEMORY_PROGRESS_STYLE", "auto").lower()


def _default_progress_file() -> Path:
    """Where _Heartbeat writes the live progress snapshot.

    Cross-process channel for the `code-memory watch` CLI: any process
    running ingest writes here on every tick; the watch CLI tails the
    same path and renders a rich live bar. Path is overridable via
    ``CODEMEMORY_PROGRESS_FILE`` for tests or split projects.
    """
    override = os.environ.get("CODEMEMORY_PROGRESS_FILE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "code-memory" / "ingest-progress.json"


_PROGRESS_FILE = _default_progress_file()


def _write_progress_snapshot(snap: dict[str, Any]) -> None:
    """Atomically write a progress snapshot for the watch CLI.

    Atomic via tmp + rename so a watcher never reads a half-written
    document. Failures swallowed — UI must not break the ingest loop.
    """
    try:
        _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap))
        os.replace(tmp, _PROGRESS_FILE)
    except Exception:  # noqa: BLE001 — UI errors must not abort ingest.
        pass


def _want_rich_progress() -> bool:
    if _PROGRESS_STYLE == "none" or not _PROGRESS_ENABLED:
        return False
    if _PROGRESS_STYLE == "rich":
        return True
    if _PROGRESS_STYLE == "text":
        return False
    try:
        return bool(sys.stderr.isatty())
    except Exception:
        return False


class _Heartbeat:
    """Render ingest progress.

    Two render paths share one API:

    * **rich** — `rich.progress.Progress` live bar on stderr with files,
      symbols, chunks, skipped counters + ETA. Used when stderr is a TTY
      (or `CODEMEMORY_PROGRESS_STYLE=rich`).
    * **text** — periodic ``files=… symbols=…`` lines on stderr. Used
      when stderr is captured (MCP stdio server, CI logs, `bash` from an
      agent harness) so ANSI escapes don't pollute the transcript.
    """

    def __init__(
        self,
        label: str,
        *,
        total: int | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self.label = label
        self.total = total
        self.start = time.monotonic()
        self.last = self.start
        self._rich: Any = None
        self._task: Any = None
        self._on_progress = on_progress
        # Throttle out-of-band progress notifications so a 50k-file ingest
        # doesn't flood the MCP transport. Rich's own refresh loop is
        # already throttled internally.
        self._cb_interval = float(
            os.environ.get("CODEMEMORY_PROGRESS_NOTIFY_INTERVAL", "0.4")
        )
        self._cb_last = 0.0
        if _want_rich_progress():
            self._init_rich()

    def _init_rich(self) -> None:
        try:
            from rich.console import Console
            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                SpinnerColumn,
                TextColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )
        except Exception:  # noqa: BLE001 — rich missing, fall back to text
            return
        progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold cyan]code-memory[/] {task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn(
                "[green]{task.fields[symbols]}[/]sym "
                "[magenta]{task.fields[chunks]}[/]chk "
                "[yellow]{task.fields[skipped]}[/]skip "
                "[dim]{task.fields[rate]}/s[/]"
            ),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=Console(stderr=True),
            transient=False,
            refresh_per_second=8,
        )
        try:
            progress.start()
        except Exception:  # noqa: BLE001
            return
        self._rich = progress
        self._task = progress.add_task(
            self.label,
            total=self.total,
            symbols=0,
            chunks=0,
            skipped=0,
            rate="0.0",
        )

    def _rate(self, files: int) -> float:
        elapsed = max(time.monotonic() - self.start, 1e-6)
        return files / elapsed

    def _snapshot(self, stats: IngestStats, *, done: bool) -> dict[str, Any]:
        return {
            "label": self.label,
            "files": stats.files,
            "total": self.total,
            "symbols": stats.symbols,
            "chunks": stats.chunks,
            "skipped": stats.skipped,
            "rate": self._rate(stats.files),
            "elapsed": time.monotonic() - self.start,
            "ts": time.time(),
            "done": done,
            "pid": os.getpid(),
        }

    def _notify(self, stats: IngestStats, *, force: bool = False) -> None:
        if self._on_progress is None:
            return
        now = time.monotonic()
        if not force and now - self._cb_last < self._cb_interval:
            return
        self._cb_last = now
        rate = self._rate(stats.files)
        msg = (
            f"{self.label}: files={stats.files} "
            f"symbols={stats.symbols} chunks={stats.chunks} "
            f"skipped={stats.skipped} rate={rate:.1f}/s"
        )
        try:
            self._on_progress(stats.files, self.total, msg)
        except Exception:  # noqa: BLE001 — never let UI break the ingest
            pass

    def tick(self, stats: IngestStats) -> None:
        self._notify(stats)
        _write_progress_snapshot(self._snapshot(stats, done=False))
        if self._rich is not None:
            self._rich.update(
                self._task,
                completed=stats.files,
                total=self.total,
                symbols=stats.symbols,
                chunks=stats.chunks,
                skipped=stats.skipped,
                rate=f"{self._rate(stats.files):.1f}",
            )
            return
        if not _PROGRESS_ENABLED or _PROGRESS_STYLE == "none":
            return
        if _PROGRESS_EVERY <= 0:
            return
        if stats.files % _PROGRESS_EVERY != 0 or stats.files == 0:
            return
        now = time.monotonic()
        rate = self._rate(stats.files)
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
        self._notify(stats, force=True)
        _write_progress_snapshot(self._snapshot(stats, done=True))
        if self._rich is not None:
            try:
                self._rich.update(
                    self._task,
                    completed=stats.files,
                    total=self.total or stats.files or 1,
                    symbols=stats.symbols,
                    chunks=stats.chunks,
                    skipped=stats.skipped,
                    rate=f"{self._rate(stats.files):.1f}",
                )
                self._rich.stop()
            except Exception:  # noqa: BLE001
                pass
            self._rich = None
            self._task = None
            return
        if not _PROGRESS_ENABLED or _PROGRESS_STYLE == "none":
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
    references: int = 0
    chunks: int = 0
    deleted: int = 0
    skipped: int = 0
    mode: str = "full"
    base_sha: str | None = None
    head_sha: str | None = None
    resolver: dict[str, int] | None = None
    sanity: dict[str, object] | None = None
    projects: dict[str, int] | None = None
    dlls: dict[str, int] | None = None
    solutions: dict[str, int] | None = None
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
        skip_vectors: bool = False,
    ) -> None:
        self.slug = project or detect_project_slug()
        self.cfg: Config = CONFIG.for_project(self.slug)
        self.skip_vectors = skip_vectors
        self.embedder = embedder or get_embedder()
        self.vector = vector or QdrantStore()
        self.graph = graph or FalkorStore(graph_name=self.cfg.falkor_graph)
        self.episodic = episodic or EpisodicStore(path=self.cfg.episodic_db)
        # Skip the Qdrant probes too when ``skip_vectors``: large-repo
        # operators who deliberately turn off the vector layer shouldn't
        # have to keep Qdrant alive.
        if not getattr(self, "skip_vectors", False):
            self.vector.ensure_collection(self.cfg.qdrant_code)
            self.vector.ensure_collection(self.cfg.qdrant_episodes)
        self.graph.ensure_indexes()
        self.state = IngestStateStore(self.cfg.episodic_db)
        # Routing target for vector upserts.  During a full rebuild this is
        # temporarily redirected to a shadow collection so the live collection
        # is never emptied before the rebuild commits.  Reset to the canonical
        # name after ``_commit_shadow_collection`` succeeds.
        self._active_code_collection: str = self.cfg.qdrant_code
        # Routing target for graph writes.  During a full rebuild this is
        # temporarily redirected to a shadow FalkorStore so the live graph is
        # never emptied before the rebuild commits.  Reset to ``self.graph``
        # after ``promote_shadow`` succeeds.
        self._active_graph: FalkorStore = self.graph
        self._closed = False

    def __enter__(self) -> "Pipeline":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Idempotent teardown of the sqlite stores this Pipeline owns.

        ``self.vector`` / ``self.graph`` are process-wide singletons
        (see ``get_qdrant_client`` / ``get_falkor_db``) and must stay
        open for other ``Pipeline`` instances in the same process —
        only the per-instance sqlite stores (episodic + ingest state)
        are closed here. A daemon wrapping each sync in
        ``with Pipeline(...) as p:`` never leaks a sqlite handle, even
        when a single ingest raises.
        """
        if self._closed:
            return
        self._closed = True
        for store in (getattr(self, "episodic", None), getattr(self, "state", None)):
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass

    def ingest_repo(
        self,
        root: str | Path,
        *,
        mode: IngestMode = "auto",
        since: str | None = None,
        dry_run: bool = False,
        on_progress: ProgressCallback | None = None,
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
            stats = self._ingest_full(
                root_path, dry_run=dry_run, on_progress=on_progress
            )
            # Resolver runs inside _ingest_full (against the shadow before
            # promotion) so we do NOT call _run_resolver here — that would
            # double-run it and unnecessarily scan the already-resolved graph.
            if is_git and not dry_run:
                self._record_state(
                    root_path, stats,
                    file_count=stats.files, symbol_count=stats.symbols,
                )
            return stats

        # git path
        if not is_git:
            raise RuntimeError(f"{root_path} is not a git repository (mode={mode!r})")

        head = git_delta.head_sha(root_path)
        branch = git_delta.current_branch(root_path)
        base = self._resolve_base(root_path, since=since, mode=mode)

        if base is None:
            # auto + git + no prior + no --since => full walk, then record state
            stats = self._ingest_full(
                root_path, dry_run=dry_run, on_progress=on_progress
            )
            stats.head_sha = head
            if self._health_check_reason:
                stats.notes.append(self._health_check_reason)
                self._health_check_reason = None
            else:
                stats.notes.append("no prior ingest state; performed full walk")
            if not dry_run:
                # Resolver runs inside _ingest_full (against the shadow before
                # promotion) so we do NOT call _run_resolver here.
                self._record_state(
                    root_path, stats, head=head, branch=branch,
                    file_count=stats.files, symbol_count=stats.symbols,
                )
            return stats

        # Incremental
        delta = git_delta.changed_since(root_path, base, include_dirty=True)
        stats = self._ingest_delta(
            root_path,
            delta,
            base_sha=base,
            head_sha=head,
            dry_run=dry_run,
            on_progress=on_progress,
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

        # Health check: verify the prior full ingest wasn't incomplete.
        if not self._health_check_ok(root, prior):
            self.state.clear(root)
            return None

        return prior.last_sha

    # Set by _health_check_ok when forcing a full re-ingest, consumed
    # by ingest_repo's stats.notes so the user sees why.
    _health_check_reason: str | None = None

    def _health_check_ok(
        self, root: Path, prior: IngestState,
    ) -> bool:
        """Return False when the prior full ingest was likely incomplete.

        Uses the ``file_count`` and ``symbol_count`` stored during the
        last full ingest.  If either is missing (legacy state) the check
        is skipped silently.  When enabled via config, the repo's current
        ingestable file count is compared against the stored value; a
        suspicious growth triggers a FalkorDB symbol-count cross-check.
        """
        if not self.cfg.ingest_health_check_enabled:
            return True
        if prior.file_count is None or prior.symbol_count is None:
            # Legacy state from before the health-check columns existed;
            # no data to compare against.
            return True

        try:
            current_file_count = _count_ingestable_files(root)
        except Exception:  # noqa: BLE001 — counting must not break ingest
            return True

        if current_file_count <= prior.file_count * 1.2:
            # File count hasn't grown suspiciously — nothing to check.
            return True

        # File count grew > 20%: cross-check against the graph.
        current_symbol_count = self.graph.count_symbols()
        if current_symbol_count == 0 and (prior.symbol_count or 0) > 0:
            # count_symbols() returns 0 on any FalkorDB error (connection
            # down, query timeout, etc.).  An empty count against a known-
            # populated prior state most likely means FalkorDB is
            # temporarily unreachable — not a broken ingest.  Treat it
            # as "healthy" to avoid triggering a destructive full rebuild
            # on every FalkorDB blip.
            return True
        expected_min = int(current_file_count * self.cfg.ingest_health_check_min_ratio)
        if current_symbol_count >= expected_min:
            return True

        self._health_check_reason = (
            f"health-check: prior full ingest recorded {prior.file_count} files "
            f"/ {prior.symbol_count} symbols; repo now has {current_file_count} "
            f"ingestable files but graph holds only {current_symbol_count} symbols "
            f"(expected ≥ {expected_min}). Forcing full re-ingest."
        )
        sys.stderr.write(f"[code-memory] {self._health_check_reason}\n")
        return False

    def _ingest_full(
        self,
        root: Path,
        *,
        dry_run: bool,
        on_progress: ProgressCallback | None = None,
    ) -> IngestStats:
        extractor = Extractor()
        stats = IngestStats(mode="full")
        sanity = SanitySummary()
        head_sha, head_ord = _resolve_head(root)
        stats.head_sha = head_sha
        # Shadow names are computed unconditionally so the finalization
        # block can reference them without a possibly-undefined warning.
        shadow_name: str = self.cfg.qdrant_code + "__shadow"
        shadow_graph_name: str = self.cfg.falkor_graph + "__shadow"
        if not dry_run:
            # Redirect vector upserts to a shadow collection so the live
            # collection is never emptied before the rebuild commits.  An
            # interrupted rebuild leaves the live collection intact — the
            # shadow is cleaned up at the start of the next rebuild attempt.
            # Remove any leftover shadow from a previous interrupted rebuild.
            self._drop_collection_if_exists(shadow_name)
            self.vector.recreate_collection(shadow_name)
            self._active_code_collection = shadow_name
            # Build graph writes into a shadow FalkorStore so the live graph
            # is never emptied before the rebuild commits.  An interrupted
            # rebuild leaves the live graph intact — the shadow is cleaned up
            # at the start of the next rebuild attempt.
            # Only engage the shadow mechanism when the live graph is a real
            # FalkorStore (or a mock/stub standing in for one in tests).
            # ``isinstance`` is wrapped in try/except because if FalkorStore
            # is itself patched in tests, its mock may not be a valid type
            # argument, raising TypeError — in that case treat as "real" so
            # the shadow logic runs with the patched factory.
            try:
                _graph_is_real = isinstance(self.graph, FalkorStore)
            except TypeError:
                _graph_is_real = True  # patched in tests — proceed with shadow
            if _graph_is_real:
                shadow_store = FalkorStore(graph_name=shadow_graph_name)
                shadow_store.drop_graph(shadow_graph_name)
                shadow_store.ensure_indexes()
                self._active_graph = shadow_store
            # Only clear ingest state (no graph wipe — the live graph stays
            # intact until promote_shadow fires at the end of a successful
            # rebuild).
            self.state.clear(root)
        hb = _Heartbeat(
            "full ingest" + (" (dry-run)" if dry_run else ""),
            on_progress=on_progress,
        )

        # Buffer chunks across files so the embedder sees a large batch
        # per call, then fan the Qdrant upserts out to a small thread
        # pool so they overlap with the next batch's embedding work.
        # On a cold ingest, embed (Ollama HTTP, serial) dominates; the
        # qdrant upsert (network + index write) blocks for ~80-150 ms
        # per batch — pipelining lets that happen while the next embed
        # batch is in flight. On a warm ingest (cache hits), embed
        # returns instantly and qdrant + graph become the path, so the
        # same pool keeps Qdrant from blocking the graph layer.
        pending_chunks: list[tuple[ExtractedFile, _Chunk]] = []
        EMBED_BATCH = 64
        UPSERT_POOL_SIZE = 2
        UPSERT_QUEUE_MAX = 4
        upsert_executor = ThreadPoolExecutor(max_workers=UPSERT_POOL_SIZE)
        in_flight: list[Future] = []

        def _await_one() -> None:
            if not in_flight:
                return
            fut = in_flight.pop(0)
            fut.result()  # propagate exceptions

        def _flush_pending() -> None:
            if not pending_chunks:
                return
            batch = list(pending_chunks)
            pending_chunks.clear()
            fut = upsert_executor.submit(self._embed_and_upsert, batch)
            in_flight.append(fut)
            # Bound queue so upserts don't fall arbitrarily behind embed.
            while len(in_flight) >= UPSERT_QUEUE_MAX:
                _await_one()

        try:
            for ex in extractor.walk(root):
                stats.files += 1
                stats.symbols += len(ex.symbols)
                stats.imports += len(ex.imports)
                stats.calls += len(ex.calls)
                stats.references += len(ex.references)
                stats.chunks += len(ex.symbols) or 1
                sanity.record(ex)
                if not dry_run:
                    # Graph upserts are cheap (UNWIND-batched per call) and
                    # need to stay per-file so the temporal stamping order
                    # matches the walk. Vector work defers to the buffer.
                    self._upsert_graph(ex, head_sha=head_sha, head_ord=head_ord)
                    if not getattr(self, "skip_vectors", False):
                        for c in _chunks_for(ex):
                            pending_chunks.append((ex, c))
                        if len(pending_chunks) >= EMBED_BATCH:
                            _flush_pending()
                hb.tick(stats)
            if not getattr(self, "skip_vectors", False):
                _flush_pending()
                # Drain the pool so the resolver + .NET-project pass sees a
                # quiescent Qdrant. Drop the pool here, not in __exit__,
                # because the .NET-project pass runs in this method.
                while in_flight:
                    _await_one()
        finally:
            # A mid-walk exception (extractor crash, embed HTTP failure,
            # etc.) must still shut down the upsert pool — leaving it
            # running leaks worker threads on every failed full ingest in
            # a long-lived daemon that retries repeatedly.
            upsert_executor.shutdown(wait=True)
        hb.done(stats)
        _attach_sanity(stats, sanity)
        self._ingest_dotnet_projects(
            root, stats, dry_run=dry_run, head_sha=head_sha, head_ord=head_ord
        )
        if not dry_run:
            # All data committed to shadow; atomically swap into the live
            # collection.  If this step fails the shadow persists and the
            # live collection is still intact from before the rebuild.
            self._commit_shadow_collection()
            active_graph = getattr(self, "_active_graph", self.graph)
            # Promote the shadow only when a shadow was actually created
            # (i.e., active_graph is a different store from self.graph).
            if active_graph is not self.graph:
                # Run the resolver against the shadow graph before promoting
                # so the live graph receives fully-resolved call edges.
                self._run_resolver_on(active_graph, stats)
                # Atomically promote the shadow graph to the live graph.
                self.graph.promote_shadow(shadow_graph_name)
                self._active_graph = self.graph
            else:
                # No graph shadow (mock/stub graph or shadow was not created).
                # Run resolver against whatever graph store is active.
                self._run_resolver_on(active_graph, stats)
        return stats

    def _run_resolver_on(self, graph_store: FalkorStore, stats: IngestStats) -> None:
        """Resolve placeholder ``name::X`` Symbol nodes on ``graph_store``.

        Records resolver stats on the ingest stats object so callers can
        see how much of the call graph is now grounded vs. ambiguous.
        Failures are non-fatal — ingest data is already persisted.

        ``graph_store`` is typically ``self.graph`` for the incremental
        path and a shadow store for the full-rebuild path (so the live
        graph stays coherent until ``promote_shadow`` fires).
        """
        try:
            r = resolve_graph(graph_store)
        except Exception as e:
            stats.notes.append(f"resolver skipped: {e}")
            return
        stats.resolver = {
            "placeholders": r.placeholders,
            "edges_total": r.edges_total,
            "resolved_same_file": r.edges_resolved_same_file,
            "resolved_imported": r.edges_resolved_imported,
            "resolved_unique": r.edges_resolved_unique,
            "resolved_assembly": r.edges_resolved_assembly,
            "ambiguous": r.edges_left_ambiguous,
            "external": r.edges_left_external,
            "placeholders_deleted": r.placeholders_deleted,
            "import_aliases_added": r.import_aliases_added,
        }

    def _run_resolver(self, stats: IngestStats) -> None:
        """Thin wrapper: resolve the live graph.

        Used by the incremental path.  The full-rebuild path calls
        ``_run_resolver_on`` directly so the resolver runs against the
        shadow before ``promote_shadow`` fires.
        """
        self._run_resolver_on(self.graph, stats)

    def _ingest_dotnet_projects(
        self,
        root: Path,
        stats: IngestStats,
        *,
        dry_run: bool,
        head_sha: str | None = None,
        head_ord: int | None = None,
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
        self._upsert_dotnet_projects(
            projects, head_sha=head_sha, head_ord=head_ord
        )
        self._index_referenced_assemblies(
            projects, stats, head_sha=head_sha, head_ord=head_ord
        )
        self._index_file_containment(
            projects, stats, head_sha=head_sha, head_ord=head_ord
        )
        self._index_solutions(
            root, stats, head_sha=head_sha, head_ord=head_ord
        )

    def _index_referenced_assemblies(
        self,
        projects: list[CsprojInfo],
        stats: IngestStats,
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
        """Parse referenced DLLs and index their public type surface.

        Layer on top of the csproj topology (PR1 shipped Project +
        Package + PackageReference edges). This step turns the logical
        ``<PackageReference>`` and ``<ProjectReference>`` into concrete
        ``.dll`` paths, parses each via :func:`code_memory.extractor.dll.parse_assembly`,
        and writes:

        * ``Assembly`` nodes keyed by ``"Name, Version=X.Y.Z.W"``. Two
          versions of the same lib stay distinct so the agent can see
          when projects pin different versions of the same dep.
        * ``Type`` nodes keyed by ``"{assembly_id}::{Namespace}.{Name}"``.
          Only public types (top-level or nested-public); private
          implementation detail stays unindexed.
        * ``USES_ASSEMBLY`` edges (Project → Assembly).
        * ``EXPOSES_TYPE`` edges (Assembly → Type).

        DLL resolution leans on the NuGet global cache plus project
        build outputs (see ``code_memory.extractor.nuget``). Failures
        are silenced: DLLs are read-only metadata, not load-bearing.
        ``stats.dlls`` carries the counters so users see how much of
        the binary surface we managed to index.
        """
        # Dedupe DLL paths across the whole solution so a shared
        # dependency parses exactly once even when many projects pull
        # the same Newtonsoft.Json on disk. ``unresolved`` counts
        # PackageReferences we couldn't locate (offline machine,
        # unrestored NuGet cache).
        path_to_consumers: dict[str, set[str]] = {}
        unresolved = 0
        for proj in projects:
            refs = resolve_refs(proj)
            for dll in refs.all_paths():
                path_to_consumers.setdefault(str(dll), set()).add(proj.path)
            for pkg in proj.package_references:
                if pkg.name not in refs.package_dlls:
                    unresolved += 1

        if not path_to_consumers:
            stats.dlls = {
                "assemblies": 0,
                "types": 0,
                "skipped": 0,
                "unresolved": unresolved,
            }
            return

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        seen_assembly_keys: set[str] = set()
        seen_type_keys: set[str] = set()
        skipped = 0

        for dll_path, consumers in path_to_consumers.items():
            info = parse_assembly(dll_path)
            if info is None:
                skipped += 1
                continue
            asm_key = info.identity
            if asm_key not in seen_assembly_keys:
                seen_assembly_keys.add(asm_key)
                asm_props: dict[str, object] = {
                    "name": info.name,
                    "version": info.version,
                    "path": info.path,
                }
                if info.public_key_token:
                    asm_props["public_key_token"] = info.public_key_token
                nodes.append(
                    GraphNode(label="Assembly", key=asm_key, props=asm_props)
                )
                for tref in info.types:
                    type_key = f"{asm_key}::{tref.namespace}.{tref.name}".rstrip(".")
                    if type_key in seen_type_keys:
                        continue
                    seen_type_keys.add(type_key)
                    type_props: dict[str, object] = {
                        "name": tref.name,
                        "namespace": tref.namespace,
                        "kind": tref.kind,
                        "sealed": tref.sealed,
                        "assembly": asm_key,
                    }
                    nodes.append(
                        GraphNode(label="Type", key=type_key, props=type_props)
                    )
                    edges.append(
                        GraphEdge(
                            type="EXPOSES_TYPE",
                            src_label="Assembly",
                            src_key=asm_key,
                            dst_label="Type",
                            dst_key=type_key,
                        )
                    )
            for consumer in consumers:
                edges.append(
                    GraphEdge(
                        type="USES_ASSEMBLY",
                        src_label="Project",
                        src_key=consumer,
                        dst_label="Assembly",
                        dst_key=asm_key,
                    )
                )

        stats.dlls = {
            "assemblies": len(seen_assembly_keys),
            "types": len(seen_type_keys),
            "skipped": skipped,
            "unresolved": unresolved,
        }
        active = getattr(self, "_active_graph", self.graph)
        active.upsert_nodes(nodes, head_sha=head_sha, head_ord=head_ord)
        active.upsert_edges(edges, head_sha=head_sha, head_ord=head_ord)

    def _index_solutions(
        self,
        root: Path,
        stats: IngestStats,
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
        """Walk `.sln` files and emit Solution nodes + Project membership.

        Schema added:

        * ``Solution`` node keyed by the solution's absolute path with
          ``name`` and ``project_count``.
        * ``MEMBER_OF`` edge from each indexed Project to the
          Solution(s) that include it. A single project can be a
          member of multiple solutions (shared infra in monorepos);
          all edges are emitted.

        Solutions whose `Project(...)` entries point at csprojs we
        didn't index (relative path goes outside the repo) end up
        with fewer ``MEMBER_OF`` edges than their declared project
        count — the discrepancy lives in ``stats.solutions``.
        """
        try:
            solutions = walk_solutions(root)
        except Exception as e:  # noqa: BLE001
            stats.notes.append(f"sln indexing skipped: {e}")
            return
        if not solutions:
            return

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        total_members = 0
        for sln in solutions:
            nodes.append(
                GraphNode(
                    label="Solution",
                    key=sln.path,
                    props={
                        "name": sln.name,
                        "project_count": len(sln.projects),
                    },
                )
            )
            for sp in sln.projects:
                total_members += 1
                edges.append(
                    GraphEdge(
                        type="MEMBER_OF",
                        src_label="Project",
                        src_key=sp.csproj_path,
                        dst_label="Solution",
                        dst_key=sln.path,
                        props={"guid": sp.guid},
                    )
                )
        stats.solutions = {
            "solutions": len(solutions),
            "memberships": total_members,
        }
        active = getattr(self, "_active_graph", self.graph)
        active.upsert_nodes(nodes, head_sha=head_sha, head_ord=head_ord)
        active.upsert_edges(edges, head_sha=head_sha, head_ord=head_ord)

    def _index_file_containment(
        self,
        projects: list[CsprojInfo],
        stats: IngestStats,
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
        """Tie each .NET source file to its owning ``Project`` node.

        The resolver needs this to answer "which assemblies can this
        file legitimately reach into" without inferring it from the
        directory tree at query time. Containment is decided by the
        **deepest** csproj whose directory is a prefix of the file's
        path — important for repos that nest sub-projects (a file
        under ``A/Sub/X.cs`` belongs to ``A/Sub`` if ``A/Sub.csproj``
        exists, not the outer ``A.csproj``).

        Files outside any csproj's directory get no edge — useful for
        scripts / loose .cs at the repo root, where ownership is
        ambiguous.

        The :class:`IngestStats` record gains ``stats.projects`` keys
        ``files_assigned`` / ``files_unowned`` so the agent can see
        coverage at a glance.
        """
        # Sort csproj dirs by path length descending so the deepest
        # prefix-match wins on a single linear scan per file.
        proj_dirs = sorted(
            ((str(Path(p.path).parent.resolve()), p.path) for p in projects),
            key=lambda x: -len(x[0]),
        )
        if not proj_dirs:
            return

        active = getattr(self, "_active_graph", self.graph)
        rows = active.graph.query(
            "MATCH (f:File) "
            "WHERE f.lang IN ['csharp', 'fsharp', 'vb', 'razor'] "
            "RETURN f.key"
        ).result_set
        files = [row[0] for row in rows]
        if not files:
            return

        edges: list[GraphEdge] = []
        assigned = 0
        unowned = 0
        for file_path in files:
            owner = _owning_project(file_path, proj_dirs)
            if owner is None:
                unowned += 1
                continue
            assigned += 1
            edges.append(
                GraphEdge(
                    type="CONTAINED_IN",
                    src_label="File",
                    src_key=file_path,
                    dst_label="Project",
                    dst_key=owner,
                )
            )
        if edges:
            active.upsert_edges(edges, head_sha=head_sha, head_ord=head_ord)

        if stats.projects is None:
            stats.projects = {}
        stats.projects["files_assigned"] = assigned
        stats.projects["files_unowned"] = unowned

    def _upsert_dotnet_projects(
        self,
        projects: list[CsprojInfo],
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
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
        active = getattr(self, "_active_graph", self.graph)
        active.upsert_nodes(nodes, head_sha=head_sha, head_ord=head_ord)
        active.upsert_edges(edges, head_sha=head_sha, head_ord=head_ord)

    def _purge_project_index(self, root: Path) -> None:
        """Wipe code vectors + graph + ingest_state for this project.

        Episodes are independent (conversation memory) and preserved.

        Note: ``_ingest_full`` no longer calls this method directly — it
        uses the shadow-collection swap (``_commit_shadow_collection``) so
        the live index is never empty during a rebuild.  This method is
        retained for callers that need a hard synchronous wipe (e.g.
        ``orchestrator/reset.py``) and is still safe to call; it just
        doesn't have the live-index safety guarantee.
        """
        self.vector.recreate_collection(self.cfg.qdrant_code)
        self.graph.clear_graph()
        self.state.clear(root)

    def _drop_collection_if_exists(self, name: str) -> None:
        """Delete ``name`` from Qdrant if it exists; no-op otherwise."""
        try:
            self.vector.client.delete_collection(collection_name=name)
        except Exception:  # noqa: BLE001
            pass

    def _commit_shadow_collection(self) -> None:
        """Atomically promote the shadow collection to be the live collection.

        After ``_ingest_full`` finishes writing to the shadow collection,
        this method:

        1. Scrolls all points from the shadow collection.
        2. Recreates (empties) the live collection — brief empty window
           bounded by the scroll-copy below, not by the full rebuild time.
        3. Bulk-upserts all shadow points into the live collection.
        4. Deletes the shadow collection.
        5. Resets ``self._active_code_collection`` back to the canonical name.

        If this method raises, the shadow collection is left intact and the
        caller can retry.  The live collection may be empty if the failure
        happened after step 2; that is still safer than the old behaviour
        of being empty for the entire rebuild duration.
        """
        shadow_name = self.cfg.qdrant_code + "__shadow"
        live_name = self.cfg.qdrant_code

        # Collect all points from the shadow collection via paginated scroll.
        from qdrant_client.http import models as qm

        all_points: list[qm.PointStruct] = []
        # ``offset`` is ``PointId | None`` per the Qdrant client but the
        # typed signature accepts ``str | int | None``; use ``Any`` to avoid
        # fighting the invariant generics of an external library.
        from typing import Union
        PointId = Union[str, int]
        offset: PointId | None = None
        batch_size = 256
        while True:
            scroll_result = self.vector.client.scroll(
                collection_name=shadow_name,
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            batch, raw_next = scroll_result
            for point in batch:
                # ``point.vector`` may be a richer type in newer qdrant-client
                # versions; we cast through ``Any`` so the PointStruct
                # constructor sees the expected union type.
                vec: qm.VectorStruct | None = point.vector  # type: ignore[assignment]
                all_points.append(
                    qm.PointStruct(
                        id=point.id,
                        vector=vec,  # type: ignore[arg-type]
                        payload=point.payload or {},
                    )
                )
            if raw_next is None:
                break
            # ``raw_next`` is typed as ``PointId`` (str | int) by the Qdrant
            # client; re-bind through the same union.
            offset = raw_next  # type: ignore[assignment]

        # Recreate the live collection (brief empty window starts here).
        self.vector.recreate_collection(live_name)

        # Bulk-upsert in batches so large indexes don't hit Qdrant payload limits.
        SWAP_BATCH = 256
        for i in range(0, len(all_points), SWAP_BATCH):
            self.vector.client.upsert(
                collection_name=live_name,
                points=all_points[i : i + SWAP_BATCH],
            )

        # Delete the shadow collection now that the live is populated.
        self._drop_collection_if_exists(shadow_name)

        # Reset routing back to the canonical collection name.
        self._active_code_collection = live_name

    def _ingest_delta(
        self,
        root: Path,
        delta: git_delta.Delta,
        *,
        base_sha: str,
        head_sha: str,
        dry_run: bool,
        on_progress: ProgressCallback | None = None,
    ) -> IngestStats:
        stats = IngestStats(mode="incremental", base_sha=base_sha, head_sha=head_sha)
        sanity = SanitySummary()
        # Resolve the ordinal once: it's a git roundtrip we'd otherwise
        # pay per-file when tombstoning deletes / stamping upserts.
        head_ord = git_delta.commit_ordinal(root, head_sha) if head_sha else None
        reingest = list(delta.reingest_paths())
        hb = _Heartbeat(
            "incremental ingest" + (" (dry-run)" if dry_run else ""),
            total=len(reingest),
            on_progress=on_progress,
        )

        for path in delta.deleted:
            path_str = str(path)
            stats.deleted += 1
            if dry_run:
                continue
            self.graph.delete_file(
                path_str, head_sha=head_sha, head_ord=head_ord
            )
            if not getattr(self, "skip_vectors", False):
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
                stats.references += len(ex.references)
                stats.chunks += len(ex.symbols) or 1
                sanity.record(ex)
                continue

            ex = self.reingest_file(path, head_sha=head_sha, head_ord=head_ord)
            if ex is None:
                stats.skipped += 1
                continue
            stats.files += 1
            stats.symbols += len(ex.symbols)
            stats.imports += len(ex.imports)
            stats.calls += len(ex.calls)
            stats.references += len(ex.references)
            stats.chunks += len(ex.symbols) or 1
            sanity.record(ex)
            hb.tick(stats)

        hb.done(stats)
        _attach_sanity(stats, sanity)
        # Re-run csproj indexing on every delta — project files are
        # tiny and the topology shifts independently of source edits.
        self._ingest_dotnet_projects(
            root, stats, dry_run=dry_run, head_sha=head_sha, head_ord=head_ord
        )
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
        file_count: int | None = None,
        symbol_count: int | None = None,
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
        self.state.set(
            root, sha=sha, branch=branch,
            file_count=file_count, symbol_count=symbol_count,
        )

    def ingest_file(
        self,
        ex: ExtractedFile,
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
        self._upsert_graph(ex, head_sha=head_sha, head_ord=head_ord)
        if not getattr(self, "skip_vectors", False):
            self._upsert_vectors(ex)

    def reingest_file(
        self,
        path: str | Path,
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> ExtractedFile | None:
        from ..extractor.treesitter import extract_file

        ex = extract_file(path)
        if ex is None:
            return None
        # When a caller doesn't know the SHA (per-file save hook), best-
        # effort resolve from the file's enclosing repo so the temporal
        # stamp still lands. Cheap: a single `git rev-parse HEAD`.
        if head_sha is None:
            head_sha, head_ord = _resolve_head(Path(ex.path).parent)
        self.graph.delete_file(ex.path, head_sha=head_sha, head_ord=head_ord)
        if not getattr(self, "skip_vectors", False):
            self.vector.delete_by_path(self.cfg.qdrant_code, ex.path)
        self.ingest_file(ex, head_sha=head_sha, head_ord=head_ord)
        return ex

    def delete_paths(
        self,
        paths: Iterable[Path | str],
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> int:
        """Remove ``paths`` from graph + vector index.

        Mirrors the deletion branch of ``ingest_delta`` so callers that
        already know which files vanished (file-save hooks, dirty-only
        sync) can prune without recomputing a full git delta. When
        ``head_sha`` is omitted we resolve it once from the first path's
        repo so the temporal stamp still lands.
        """
        path_list = [str(p) for p in paths]
        if not path_list:
            return 0
        if head_sha is None and path_list:
            head_sha, head_ord = _resolve_head(Path(path_list[0]).parent)
        for path_str in path_list:
            self.graph.delete_file(path_str, head_sha=head_sha, head_ord=head_ord)
            if not getattr(self, "skip_vectors", False):
                self.vector.delete_by_path(self.cfg.qdrant_code, path_str)
        return len(path_list)

    def record_episode(self, ep: Episode) -> str:
        ep_id = self.episodic.add(ep)
        hv = self.embedder.embed_one(episode_text(ep))
        self.vector.upsert(
            self.cfg.qdrant_episodes,
            [VectorRecord(id=ep_id, vector=hv, payload=episode_payload(ep))],
        )
        return ep_id

    def dedupe_episodes(self) -> dict[str, int]:
        """Compact duplicate episodes in SQLite and prune their vectors.

        Mirrors ``EpisodicStore.dedupe`` and follows up with a Qdrant
        delete for removed point ids so the vector store doesn't drift
        from the source of truth. Returns ``{"removed": n, "groups": g}``.
        """
        removed_map = self.episodic.dedupe()
        removed_ids: list[str] = []
        for ids in removed_map.values():
            removed_ids.extend(ids)
        if removed_ids and not getattr(self, "skip_vectors", False):
            self.vector.delete_by_ids(self.cfg.qdrant_episodes, removed_ids)
        return {"removed": len(removed_ids), "groups": len(removed_map)}

    def _upsert_graph(
        self,
        ex: ExtractedFile,
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
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
            if s.param_count is not None:
                props["params"] = s.param_count
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

        # Calls are now (name, arity) pairs. Dedupe on the pair so two
        # call sites of ``Run()`` collapse, but ``Run()`` and ``Run(x)``
        # both contribute their own edges — the resolver uses the
        # arity downstream to disambiguate overloads.
        seen_calls: set[tuple[str, int, str | None]] = set()
        for call in ex.calls:
            key_triple = (call.name, call.arity, call.receiver_type)
            if key_triple in seen_calls:
                continue
            seen_calls.add(key_triple)
            call_props: dict[str, Any] = {
                "unresolved": True,
                "args": call.arity,
            }
            if call.receiver_type:
                call_props["receiver_type"] = call.receiver_type
            edges.append(
                GraphEdge(
                    type="CALLS",
                    src_label="File",
                    src_key=ex.path,
                    dst_label="Symbol",
                    dst_key=f"name::{call.name}",
                    props=call_props,
                )
            )
            nodes.append(
                GraphNode(
                    label="Symbol",
                    key=f"name::{call.name}",
                    props={"name": call.name, "unresolved": True},
                )
            )

        # Type-position references (base lists, parameter types, field/
        # property types, generics, type constraints, cast/is/as/typeof
        # targets). Emitted as a separate REFERENCES edge type so the
        # graph keeps the semantic distinction from CALLS (`X invokes Y`)
        # while letting "who touches type X" queries union them.
        seen_refs: set[str] = set()
        for ref in ex.references:
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            edges.append(
                GraphEdge(
                    type="REFERENCES",
                    src_label="File",
                    src_key=ex.path,
                    dst_label="Symbol",
                    dst_key=f"name::{ref}",
                    props={"unresolved": True},
                )
            )
            nodes.append(
                GraphNode(
                    label="Symbol",
                    key=f"name::{ref}",
                    props={"name": ref, "unresolved": True},
                )
            )

        # Razor / Blazor DI: emit INJECTS edges to the same placeholder
        # Symbol pool so the resolver can rewrite them to real Type /
        # Symbol targets in the same pass that handles calls. Keeping
        # the edge type distinct preserves the semantic ("X is a DI
        # dependency of this file", not "X is called by this file").
        seen_injects: set[str] = set()
        for injected in ex.injects:
            if injected in seen_injects:
                continue
            seen_injects.add(injected)
            edges.append(
                GraphEdge(
                    type="INJECTS",
                    src_label="File",
                    src_key=ex.path,
                    dst_label="Symbol",
                    dst_key=f"name::{injected}",
                    props={"unresolved": True},
                )
            )
            nodes.append(
                GraphNode(
                    label="Symbol",
                    key=f"name::{injected}",
                    props={"name": injected, "unresolved": True},
                )
            )

        active = getattr(self, "_active_graph", self.graph)
        active.upsert_nodes(nodes, head_sha=head_sha, head_ord=head_ord)
        active.upsert_edges(edges, head_sha=head_sha, head_ord=head_ord)

    def _embed_and_upsert(
        self, pending: list[tuple[ExtractedFile, _Chunk]]
    ) -> None:
        """Embed and persist a cross-file chunk batch in one shot.

        Used by the full-ingest hot path so the embedder receives a
        large list per call (avoiding per-file HTTP overhead) and
        Qdrant gets a single bulk-upsert. Order of records mirrors the
        input so the embedder result vector aligns 1:1.
        """
        if not pending:
            return
        texts = [c.text for _, c in pending]
        hvecs = self.embedder.embed(texts)
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
            for (ex, c), hv in zip(pending, hvecs, strict=True)
        ]
        self.vector.upsert(
            getattr(self, "_active_code_collection", self.cfg.qdrant_code),
            records,
        )

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
            self.vector.upsert(
                getattr(self, "_active_code_collection", self.cfg.qdrant_code),
                records,
            )


def _count_ingestable_files(root: Path) -> int:
    """Fast count of ingestable files under *root* without parsing.

    Uses the same ignore logic as ``Extractor.walk`` (default ignore
    dirs + gitignore).  Only files with extensions recognised by
    ``LANG_BY_EXT`` are counted.  No tree-sitter parsing is done.
    """
    from ..extractor.gitignore import GitignoreMatcher

    root_path = root.resolve()
    matcher = GitignoreMatcher.from_root(root_path)
    ignore_set = set(DEFAULT_IGNORE_DIRS)
    supported_exts = set(LANG_BY_EXT.keys())
    count = 0
    for p in root_path.rglob("*"):
        if not p.is_file():
            continue
        if any(part in ignore_set for part in p.parts):
            continue
        if matcher.match(p, is_dir=False):
            continue
        if p.suffix.lower() not in supported_exts:
            continue
        count += 1
    return count


def _resolve_head(root: str | Path) -> tuple[str | None, int | None]:
    """Best-effort ``(head_sha, head_ord)`` for ``root``.

    Returns ``(None, None)`` for non-git directories so callers can
    fall through to legacy unstamped behaviour. The ordinal is the
    first-parent commit count (``git rev-list --count --first-parent``),
    which gives a monotonic integer along the trunk — usable as a
    cheap "before/after" comparator without pulling the whole topology
    into the graph.
    """
    p = Path(root)
    if not git_delta.is_git_repo(p):
        return None, None
    try:
        sha = git_delta.head_sha(p)
    except git_delta.GitError:
        return None, None
    if not sha:
        return None, None
    return sha, git_delta.commit_ordinal(p, sha)


def _owning_project(
    file_path: str, proj_dirs: list[tuple[str, str]]
) -> str | None:
    """Return the project key whose directory is the deepest prefix of ``file_path``.

    ``proj_dirs`` must already be sorted by descending directory-length
    so the first match wins. ``None`` means the file lives outside any
    indexed project.
    """
    abs_path = Path(file_path).resolve().as_posix()
    for dir_, proj_key in proj_dirs:
        # Match on the directory boundary (``dir/file.cs``) — substring
        # without the trailing separator would treat ``/A/B.csproj`` as
        # owning files under ``/A/Beta/`` which it doesn't. Both sides are
        # compared in posix form: ``proj_dirs`` carries native separators
        # on Windows, and a literal ``/`` never prefix-matches those.
        prefix = Path(dir_).as_posix().rstrip("/") + "/"
        if abs_path.startswith(prefix):
            return proj_key
    return None


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
