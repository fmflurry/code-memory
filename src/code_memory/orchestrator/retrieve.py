from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import CONFIG, Config, detect_project_slug
from ..embed import OllamaEmbedder
from ..episodic import Episode, EpisodicStore
from ..graph import FalkorStore
from ..vector import QdrantStore, VectorHit


@dataclass
class ContextPack:
    query: str
    code_hits: list[VectorHit] = field(default_factory=list)
    episode_hits: list[VectorHit] = field(default_factory=list)
    episodes: list[Episode] = field(default_factory=list)
    graph_expansion: list[dict[str, Any]] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"# Query\n{self.query}\n"]
        if self.code_hits:
            lines.append("## Code")
            for h in self.code_hits:
                p = h.payload
                lines.append(
                    f"- {p.get('path')}:{p.get('start')}-{p.get('end')} "
                    f"[{p.get('kind')} {p.get('name')}] score={h.score:.3f}"
                )
        if self.episode_hits:
            lines.append("\n## Episodes")
            for ep in self.episodes:
                lines.append(
                    f"- {ep.id} verdict={ep.verdict} :: {ep.prompt[:120]}"
                )
        if self.graph_expansion:
            lines.append("\n## Graph neighbors")
            for n in self.graph_expansion[:25]:
                lines.append(f"- {n['labels']} {n['key']}")
        return "\n".join(lines)


class Retriever:
    def __init__(
        self,
        project: str | None = None,
        embedder: OllamaEmbedder | None = None,
        vector: QdrantStore | None = None,
        graph: FalkorStore | None = None,
        episodic: EpisodicStore | None = None,
    ) -> None:
        self.slug = project or detect_project_slug()
        self.cfg: Config = CONFIG.for_project(self.slug)
        self.embedder = embedder or OllamaEmbedder()
        self.vector = vector or QdrantStore()
        self.graph = graph or FalkorStore(graph_name=self.cfg.falkor_graph)
        self.episodic = episodic or EpisodicStore(path=self.cfg.episodic_db)

    def retrieve(
        self,
        query: str,
        top_k_code: int = 8,
        top_k_eps: int = 5,
        graph_depth: int = 1,
    ) -> ContextPack:
        qvec = self.embedder.embed_one(query)
        code_hits = self.vector.search(self.cfg.qdrant_code, qvec, top_k=top_k_code)
        ep_hits = self.vector.search(self.cfg.qdrant_episodes, qvec, top_k=top_k_eps)
        episodes = self.episodic.by_ids([h.id for h in ep_hits])

        graph_expansion: list[dict[str, Any]] = []
        seen = set()
        for h in code_hits:
            path = h.payload.get("path")
            if not path or path in seen:
                continue
            seen.add(path)
            graph_expansion.extend(self.graph.neighbors("File", path, depth=graph_depth))

        return ContextPack(
            query=query,
            code_hits=code_hits,
            episode_hits=ep_hits,
            episodes=episodes,
            graph_expansion=graph_expansion,
        )
