from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import os

from ..config import CONFIG, Config, detect_project_slug
from ..embed import M3Embedder, get_embedder
from ..episodic import Episode, EpisodicStore
from ..vector import QdrantStore, VectorHit
from .rerank import maybe_cross_encode

# Hybrid (dense + sparse RRF) is opt-in. Benchmarks on the sample-webapp
# Angular corpus showed dense-only m3 outperforms hybrid on natural
# language queries (see docs/BENCHMARK.md). Sparse over-promotes spec
# files and generated API stubs whose identifier vocabulary overlaps
# heavily with the query. The collection still stores both vectors so
# users can toggle at query time without re-ingesting.
ENV_HYBRID = "CODEMEMORY_HYBRID"


def _retrieval_mode() -> str:
    raw = os.environ.get(ENV_HYBRID, "0").strip().lower()
    return "hybrid" if raw in ("1", "true", "on", "yes") else "dense"

# Per-hit score adjustments applied after Qdrant's cosine ranking.
GENERATED_PENALTY = 0.15  # subtract from generated code hits
ENTRYPOINT_BOOST = 0.05  # add to framework entrypoint files

# Path patterns that match Angular / Node framework entrypoints worth surfacing
# even when symbol-level similarity is lower than other hits.
_ENTRYPOINT_BASENAMES = frozenset(
    {
        "app.config.ts",
        "app.routes.ts",
        "app-routing.module.ts",
        "main.ts",
        "main.server.ts",
        "app.module.ts",
        "app.component.ts",
        "index.ts",
        "providers.ts",
    }
)
_ENTRYPOINT_SUFFIXES = (".module.ts", ".routing.ts", ".routes.ts", ".config.ts")


def _is_entrypoint(path: str | None) -> bool:
    if not path:
        return False
    name = Path(path).name.lower()
    if name in _ENTRYPOINT_BASENAMES:
        return True
    return any(name.endswith(suf) for suf in _ENTRYPOINT_SUFFIXES)


def _normalize_prompt(text: str) -> str:
    """Lowercase + collapse whitespace for cheap near-duplicate detection."""
    return re.sub(r"\s+", " ", text.strip().lower())[:160]


@dataclass
class ContextPack:
    """Orientation payload for a natural-language query.

    Topology questions (who calls X, who imports Y, …) deliberately do
    **not** live here — they have dedicated MCP tools so the agent can
    issue precise graph queries instead of skimming a noisy neighbor
    dump.
    """

    query: str
    code_hits: list[VectorHit] = field(default_factory=list)
    episode_hits: list[VectorHit] = field(default_factory=list)
    episodes: list[Episode] = field(default_factory=list)

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
        lines.append(
            "\n_For topology (callers/callees/importers/dependencies/definitions) "
            "use the dedicated codememory_* tools._"
        )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Machine-readable representation for plugin / tool consumers."""
        return {
            "query": self.query,
            "code": [
                {
                    "path": h.payload.get("path"),
                    "start": h.payload.get("start"),
                    "end": h.payload.get("end"),
                    "kind": h.payload.get("kind"),
                    "name": h.payload.get("name"),
                    "score": h.score,
                }
                for h in self.code_hits
            ],
            "episodes": [
                {
                    "id": ep.id,
                    "verdict": ep.verdict,
                    "prompt": ep.prompt[:240],
                    "score": next(
                        (h.score for h in self.episode_hits if h.id == ep.id),
                        None,
                    ),
                }
                for ep in self.episodes
            ],
        }


class Retriever:
    """Vector + episode retrieval. Topology lives in dedicated MCP tools."""

    def __init__(
        self,
        project: str | None = None,
        embedder: M3Embedder | None = None,
        vector: QdrantStore | None = None,
        episodic: EpisodicStore | None = None,
    ) -> None:
        self.slug = project or detect_project_slug()
        self.cfg: Config = CONFIG.for_project(self.slug)
        self.embedder = embedder or get_embedder()
        self.vector = vector or QdrantStore()
        self.episodic = episodic or EpisodicStore(path=self.cfg.episodic_db)

    def retrieve(
        self,
        query: str,
        top_k_code: int = 8,
        top_k_eps: int = 5,
        include_idle_episodes: bool = False,
    ) -> ContextPack:
        qvec = self.embedder.embed_one(query)
        # Fetch 2x candidates so re-rank has room to lift entrypoints
        # and demote generated code without losing depth. Mode is
        # selected per ``CODEMEMORY_HYBRID``; default ``dense`` reflects
        # the benchmark winner — see docs/BENCHMARK.md.
        raw_code = self.vector.search(
            self.cfg.qdrant_code,
            qvec,
            top_k=top_k_code * 2,
            mode=_retrieval_mode(),
        )
        # Cross-encoder rerank when Metal/CUDA is available — no-op
        # otherwise. Heuristic boosts then apply on top of the new scores.
        reranked = maybe_cross_encode(query, raw_code)
        code_hits = _rerank_code(reranked)[:top_k_code]

        raw_eps = self.vector.search(
            self.cfg.qdrant_episodes, qvec, top_k=top_k_eps * 3
        )
        episodes = self.episodic.by_ids([h.id for h in raw_eps])
        ep_hits, episodes = _filter_episodes(
            query,
            raw_eps,
            episodes,
            limit=top_k_eps,
            include_idle=include_idle_episodes,
        )

        return ContextPack(
            query=query,
            code_hits=code_hits,
            episode_hits=ep_hits,
            episodes=episodes,
        )


def _rerank_code(hits: list[VectorHit]) -> list[VectorHit]:
    """Apply generated-code penalty + entrypoint boost; resort by score.

    Returns new ``VectorHit`` instances so caller doesn't see mutated cosine
    scores from Qdrant.
    """
    adjusted: list[VectorHit] = []
    for h in hits:
        score = h.score
        path = h.payload.get("path")
        if h.payload.get("generated"):
            score -= GENERATED_PENALTY
        if _is_entrypoint(path):
            score += ENTRYPOINT_BOOST
        adjusted.append(VectorHit(id=h.id, score=score, payload=h.payload))
    adjusted.sort(key=lambda h: h.score, reverse=True)
    return adjusted


def _filter_episodes(
    query: str,
    hits: list[VectorHit],
    episodes: list[Episode],
    *,
    limit: int,
    include_idle: bool,
) -> tuple[list[VectorHit], list[Episode]]:
    """Drop idle verdicts (opt-in) and dedupe near-identical prompts.

    Episodes whose normalized prompt prefix matches the current query or
    another already-kept episode are suppressed. This eliminates the
    "10 copies of my own prior question" noise without needing a second
    embedding round-trip.
    """
    by_id = {ep.id: ep for ep in episodes}
    query_key = _normalize_prompt(query)
    kept_hits: list[VectorHit] = []
    kept_eps: list[Episode] = []
    seen_keys: set[str] = {query_key}
    for h in hits:
        ep = by_id.get(h.id)
        if ep is None:
            continue
        if not include_idle and (ep.verdict or "").lower() == "idle":
            continue
        key = _normalize_prompt(ep.prompt or "")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        kept_hits.append(h)
        kept_eps.append(ep)
        if len(kept_hits) >= limit:
            break
    return kept_hits, kept_eps
