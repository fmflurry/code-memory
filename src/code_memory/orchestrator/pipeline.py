from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..config import CONFIG
from ..embed import OllamaEmbedder
from ..episodic import Episode, EpisodicStore
from ..episodic.sqlite_store import episode_payload, episode_text
from ..extractor import ExtractedFile, Extractor, Symbol
from ..graph import FalkorStore, GraphEdge, GraphNode
from ..vector import QdrantStore, VectorRecord


def _id(*parts: str) -> str:
    h = hashlib.sha1("\x00".join(parts).encode()).hexdigest()
    return h[:32]


@dataclass
class IngestStats:
    files: int = 0
    symbols: int = 0
    imports: int = 0
    calls: int = 0
    chunks: int = 0


class Pipeline:
    """Coordinator: extractor -> graph + vectors + episodes."""

    def __init__(
        self,
        embedder: OllamaEmbedder | None = None,
        vector: QdrantStore | None = None,
        graph: FalkorStore | None = None,
        episodic: EpisodicStore | None = None,
    ) -> None:
        self.embedder = embedder or OllamaEmbedder()
        self.vector = vector or QdrantStore()
        self.graph = graph or FalkorStore()
        self.episodic = episodic or EpisodicStore()
        self.vector.ensure_collection(CONFIG.qdrant_code)
        self.vector.ensure_collection(CONFIG.qdrant_episodes)
        self.graph.ensure_indexes()

    def ingest_repo(self, root: str | Path) -> IngestStats:
        extractor = Extractor()
        stats = IngestStats()
        for ex in extractor.walk(root):
            self.ingest_file(ex)
            stats.files += 1
            stats.symbols += len(ex.symbols)
            stats.imports += len(ex.imports)
            stats.calls += len(ex.calls)
            stats.chunks += len(ex.symbols) or 1
        return stats

    def ingest_file(self, ex: ExtractedFile) -> None:
        self._upsert_graph(ex)
        self._upsert_vectors(ex)

    def reingest_file(self, path: str | Path) -> ExtractedFile | None:
        from ..extractor.treesitter import extract_file

        ex = extract_file(path)
        if ex is None:
            return None
        self.graph.delete_file(ex.path)
        self.vector.delete_by_path(CONFIG.qdrant_code, ex.path)
        self.ingest_file(ex)
        return ex

    def record_episode(self, ep: Episode) -> str:
        ep_id = self.episodic.add(ep)
        vec = self.embedder.embed_one(episode_text(ep))
        self.vector.upsert(
            CONFIG.qdrant_episodes,
            [VectorRecord(id=ep_id, vector=vec, payload=episode_payload(ep))],
        )
        return ep_id

    def _upsert_graph(self, ex: ExtractedFile) -> None:
        file_node = GraphNode(label="File", key=ex.path, props={"lang": ex.lang})
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
            vectors = self.embedder.embed([c.text for c in batch])
            records = [
                VectorRecord(
                    id=_id(ex.path, c.key),
                    vector=v,
                    payload={
                        "path": ex.path,
                        "lang": ex.lang,
                        "kind": c.kind,
                        "name": c.name,
                        "start": c.start,
                        "end": c.end,
                    },
                )
                for c, v in zip(batch, vectors, strict=True)
            ]
            self.vector.upsert(CONFIG.qdrant_code, records)


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


MAX_SNIPPET_CHARS = 4000


def _symbol_text(s: Symbol, path: str) -> str:
    snippet = s.snippet[:MAX_SNIPPET_CHARS]
    return f"FILE {path}\nKIND {s.kind} NAME {s.name}\n{snippet}"
