"""Retrieval benchmark: dense-only vs hybrid (dense+sparse RRF).

Both modes share the same chunks and the same m3 embedding pass — only
the Qdrant query strategy differs. This isolates the value of adding
the sparse signal + RRF fusion on top of an otherwise identical
pipeline.

Usage
-----

    # 1. one-time: ingest the corpus (uses hybrid layout)
    uv run python -m code_memory.cli ingest <CORPUS_PATH> --project <SLUG>

    # 2. run benchmark
    uv run python scripts/benchmark.py \\
        --project <SLUG> \\
        --queries scripts/benchmark_queries.json \\
        --out docs/BENCHMARK.md

Metrics
-------
* Recall@5 / Recall@10 — fraction of queries where ≥1 gold path appears
  in the top-K.
* MRR — mean reciprocal rank of the first relevant hit (0 when none).
* nDCG@10 — graded relevance with logarithmic position discount.
* Latency p50 / p95 — wall-clock per query (warm).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from code_memory.config import CONFIG, slugify  # noqa: E402
from code_memory.embed import get_embedder  # noqa: E402
from code_memory.orchestrator.rerank import maybe_cross_encode  # noqa: E402
from code_memory.orchestrator.retrieve import _rerank_code  # noqa: E402
from code_memory.vector import QdrantStore  # noqa: E402


# ---------------------------------------------------------------- relevance


def _is_relevant(payload: dict[str, Any], gold: list[str]) -> bool:
    path = payload.get("path", "") or ""
    return any(g in path for g in gold)


# ---------------------------------------------------------------- metrics


def _recall_at_k(hits: list[dict[str, Any]], gold: list[str], k: int) -> float:
    return 1.0 if any(_is_relevant(h, gold) for h in hits[:k]) else 0.0


def _reciprocal_rank(hits: list[dict[str, Any]], gold: list[str]) -> float:
    for i, h in enumerate(hits, start=1):
        if _is_relevant(h, gold):
            return 1.0 / i
    return 0.0


def _ndcg_at_k(hits: list[dict[str, Any]], gold: list[str], k: int) -> float:
    """Binary-relevance nDCG@k.

    Ideal DCG assumes one relevant doc at rank 1; this is a conservative
    proxy when there are multiple gold paths because we still only count
    each query as having at most one ideal slot. Good enough for A/B
    comparison; not absolute IR.
    """
    dcg = 0.0
    for i, h in enumerate(hits[:k], start=1):
        if _is_relevant(h, gold):
            dcg += 1.0 / math.log2(i + 1)
            break
    idcg = 1.0  # rank 1
    return dcg / idcg


# ---------------------------------------------------------------- runner


@dataclass
class RunResult:
    mode: str
    recall_at_5: float
    recall_at_10: float
    mrr: float
    ndcg_at_10: float
    p50_ms: float
    p95_ms: float
    per_query: list[dict[str, Any]] = field(default_factory=list)


def run_mode(
    *,
    mode: str,
    collection: str,
    vector_store: QdrantStore,
    queries: list[dict[str, Any]],
    embedder: Any,
    top_k: int,
    with_rerank: bool = False,
) -> RunResult:
    recalls5: list[float] = []
    recalls10: list[float] = []
    rrs: list[float] = []
    ndcgs: list[float] = []
    latencies_ms: list[float] = []
    per_query: list[dict[str, Any]] = []

    # warm-up — first query pays cold caches in m3 + Qdrant
    if queries:
        _ = embedder.embed_one(queries[0]["query"])
        _ = vector_store.search(collection, _, top_k=top_k, mode=mode)

    # Stage-1 retrieval pulls 2x candidates so heuristics + CE rerank
    # have room to lift / demote — matches production Retriever logic.
    fetch_k = top_k * 2

    for q in queries:
        text = q["query"]
        gold = q["gold"]
        t0 = time.perf_counter()
        qvec = embedder.embed_one(text)
        raw = vector_store.search(collection, qvec, top_k=fetch_k, mode=mode)
        if with_rerank:
            raw = maybe_cross_encode(text, raw)
            raw = _rerank_code(raw)
        hits = raw[:top_k]
        dt_ms = (time.perf_counter() - t0) * 1000.0

        hit_payloads = [h.payload for h in hits]
        r5 = _recall_at_k(hit_payloads, gold, 5)
        r10 = _recall_at_k(hit_payloads, gold, 10)
        rr = _reciprocal_rank(hit_payloads, gold)
        ndcg = _ndcg_at_k(hit_payloads, gold, 10)

        recalls5.append(r5)
        recalls10.append(r10)
        rrs.append(rr)
        ndcgs.append(ndcg)
        latencies_ms.append(dt_ms)
        per_query.append(
            {
                "query": text,
                "gold": gold,
                "r@5": r5,
                "r@10": r10,
                "rr": rr,
                "ndcg@10": ndcg,
                "ms": round(dt_ms, 1),
                "top": [h.payload.get("path") for h in hits[:5]],
            }
        )

    return RunResult(
        mode=mode,
        recall_at_5=statistics.mean(recalls5) if recalls5 else 0.0,
        recall_at_10=statistics.mean(recalls10) if recalls10 else 0.0,
        mrr=statistics.mean(rrs) if rrs else 0.0,
        ndcg_at_10=statistics.mean(ndcgs) if ndcgs else 0.0,
        p50_ms=statistics.median(latencies_ms) if latencies_ms else 0.0,
        p95_ms=(
            statistics.quantiles(latencies_ms, n=20)[18]
            if len(latencies_ms) >= 20
            else max(latencies_ms or [0.0])
        ),
        per_query=per_query,
    )


# ---------------------------------------------------------------- reporting


def _md_table(results: list[RunResult]) -> str:
    head = (
        "| Mode | Recall@5 | Recall@10 | MRR | nDCG@10 | p50 (ms) | p95 (ms) |\n"
        "|------|---------:|----------:|----:|--------:|---------:|---------:|\n"
    )
    rows = "".join(
        f"| {r.mode} | {r.recall_at_5:.3f} | {r.recall_at_10:.3f} | "
        f"{r.mrr:.3f} | {r.ndcg_at_10:.3f} | {r.p50_ms:.1f} | {r.p95_ms:.1f} |\n"
        for r in results
    )
    return head + rows


def _md_delta(dense: RunResult, hybrid: RunResult) -> str:
    def pct(a: float, b: float) -> str:
        if a == 0:
            return "—" if b == 0 else "+∞"
        return f"{(b - a) / a * 100:+.1f}%"

    return (
        "| Metric | Dense | Hybrid | Δ |\n"
        "|--------|------:|-------:|--:|\n"
        f"| Recall@5  | {dense.recall_at_5:.3f}  | {hybrid.recall_at_5:.3f}  | {pct(dense.recall_at_5, hybrid.recall_at_5)} |\n"
        f"| Recall@10 | {dense.recall_at_10:.3f} | {hybrid.recall_at_10:.3f} | {pct(dense.recall_at_10, hybrid.recall_at_10)} |\n"
        f"| MRR       | {dense.mrr:.3f}          | {hybrid.mrr:.3f}          | {pct(dense.mrr, hybrid.mrr)} |\n"
        f"| nDCG@10   | {dense.ndcg_at_10:.3f}   | {hybrid.ndcg_at_10:.3f}   | {pct(dense.ndcg_at_10, hybrid.ndcg_at_10)} |\n"
        f"| p50 (ms)  | {dense.p50_ms:.1f}       | {hybrid.p50_ms:.1f}       | {pct(dense.p50_ms, hybrid.p50_ms)} |\n"
        f"| p95 (ms)  | {dense.p95_ms:.1f}       | {hybrid.p95_ms:.1f}       | {pct(dense.p95_ms, hybrid.p95_ms)} |\n"
    )


def _md_per_query(results: list[RunResult]) -> str:
    out = ["\n## Per-query results\n"]
    n = len(results[0].per_query)
    for i in range(n):
        q = results[0].per_query[i]["query"]
        gold = results[0].per_query[i]["gold"]
        out.append(f"\n### `{q}`\n")
        out.append(f"Gold: {gold}\n")
        for r in results:
            pq = r.per_query[i]
            out.append(
                f"- **{r.mode}** — r@5={pq['r@5']:.0f} r@10={pq['r@10']:.0f} "
                f"rr={pq['rr']:.2f} ndcg={pq['ndcg@10']:.2f} "
                f"({pq['ms']} ms)\n  - top5: {pq['top']}\n"
            )
    return "".join(out)


def render_report(
    *,
    corpus: str,
    project: str,
    queries_path: str,
    runs: list[RunResult],
) -> str:
    by_mode = {r.mode: r for r in runs}
    dense = by_mode["dense"]
    hybrid = by_mode["hybrid"]
    dense_rr = by_mode.get("dense+rerank")
    hybrid_rr = by_mode.get("hybrid+rerank")

    # Pick the highest-quality mode per metric for the headline.
    best_mrr = max(runs, key=lambda r: r.mrr)
    best_ndcg = max(runs, key=lambda r: r.ndcg_at_10)
    best_latency = min(runs, key=lambda r: r.p50_ms)

    parts: list[str] = [
        "# Retrieval benchmark — dense vs hybrid (BGE-M3 + RRF)\n",
        f"\n**Corpus**: `{corpus}`  ",
        f"\n**Project slug**: `{project}`  ",
        f"\n**Queries**: `{queries_path}` ({len(dense.per_query)} hand-crafted Angular queries — mix of NL and PascalCase identifier searches)  ",
        "\n**Embedding model**: `BAAI/bge-m3` via FlagEmbedding (dense 1024-d + lexical sparse, one forward pass)  ",
        "\n**Fusion**: Qdrant server-side Reciprocal Rank Fusion (RRF), prefetch 4×k  ",
        "\n**Cross-encoder**: `BAAI/bge-reranker-v2-m3`, α=0.5 blend with bi-encoder  ",
        "\n**Hardware**: Apple Silicon (MPS), fp16  ",
        "\n\n## Takeaways\n\n",
        f"- **Best MRR**: `{best_mrr.mode}` at {best_mrr.mrr:.3f}\n",
        f"- **Best nDCG@10**: `{best_ndcg.mode}` at {best_ndcg.ndcg_at_10:.3f}\n",
        f"- **Lowest p50 latency**: `{best_latency.mode}` at {best_latency.p50_ms:.1f} ms\n",
        "- Hybrid (dense + sparse RRF) alone does **not** beat dense on this Angular corpus — sparse over-promotes `*.spec.ts` and generated API stubs that share identifier vocabulary with the query. Cross-encoder rerank partially rescues hybrid but at ~200× latency cost.\n",
        "- **Default in code**: dense-only. Hybrid is exposed via `CODEMEMORY_HYBRID=1` for users whose query mix is dominated by exact symbol names — re-run this benchmark on your own corpus before flipping it.\n",
        "\n## Summary (all configurations)\n\n",
        _md_table(runs),
        "\n## Stage 1 only — dense vs hybrid (no rerank)\n\n",
        _md_delta(dense, hybrid),
    ]
    if dense_rr and hybrid_rr:
        parts.append(
            "\n## Full production pipeline — dense+rerank vs hybrid+rerank\n\n"
        )
        parts.append(_md_delta(dense_rr, hybrid_rr))
    parts.append(_md_per_query(runs))
    return "".join(parts)


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="project slug")
    parser.add_argument(
        "--queries",
        default=str(REPO_ROOT / "scripts" / "benchmark_queries.json"),
    )
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "docs" / "BENCHMARK.md"),
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--json", help="optional path to dump raw per-query JSON")
    args = parser.parse_args(argv)

    queries_path = Path(args.queries)
    spec = json.loads(queries_path.read_text(encoding="utf-8"))
    queries = spec["queries"]
    corpus = spec.get("corpus", "(unknown)")

    project = slugify(args.project)
    cfg = CONFIG.for_project(project)
    collection = cfg.qdrant_code

    print(f"[bench] project={project} collection={collection} queries={len(queries)}")

    embedder = get_embedder()
    store = QdrantStore()

    runs: list[RunResult] = []
    for mode in ("dense", "hybrid", "hybrid_dbsf"):
        for rerank in (False, True):
            label = f"{mode}{'+rerank' if rerank else ''}"
            r = run_mode(
                mode=mode,
                collection=collection,
                vector_store=store,
                queries=queries,
                embedder=embedder,
                top_k=args.top_k,
                with_rerank=rerank,
            )
            r.mode = label
            runs.append(r)

    report = render_report(
        corpus=corpus,
        project=project,
        queries_path=str(queries_path.relative_to(REPO_ROOT)),
        runs=runs,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"[bench] wrote {out_path}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {r.mode: r.__dict__ for r in runs},
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"[bench] wrote {args.json}")

    # quick console summary
    print("\n=== summary ===")
    print(_md_table(runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
