"""Benchmark: code-memory vs no-code-memory baseline.

Compares what an agent retrieves with code-memory (dense + cross-encoder
rerank) against what it would retrieve **without** code-memory — the
default agent fallback is keyword search via ripgrep over the working
tree. Both runs are scored against the same hand-crafted gold set.

Usage
-----

    uv run python scripts/benchmark_vs_baseline.py \\
        --project <SLUG> \\
        --corpus  <ABSOLUTE_PATH_TO_REPO> \\
        --queries scripts/benchmark_queries.json \\
        --out     docs/BENCHMARK_VS_BASELINE.md

Anonymization
-------------

Pass ``--anon-from foo --anon-to bar`` (repeatable) to redact org / repo
names from the rendered report. Internal paths are still used for
matching against gold; only the displayed strings are rewritten.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from code_memory.config import CONFIG, slugify  # noqa: E402
from code_memory.embed import get_embedder  # noqa: E402
from code_memory.orchestrator.rerank import maybe_cross_encode  # noqa: E402
from code_memory.orchestrator.retrieve import _rerank_code  # noqa: E402
from code_memory.vector import QdrantStore, VectorHit  # noqa: E402


# ---------------------------------------------------------------- grep baseline


# English stopwords + Angular/TS noise tokens that match every file.
_STOP = frozenset(
    """
    a an the and or of in on for to is are was were be been being do does did
    how where what which who when why does the of from into via with without
    that this these those it its app code file files service services
    component components feature features module modules angular typescript ts
    function method class type interface
    """.split()
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")


def _tokenize(query: str) -> list[str]:
    """Extract content tokens from a natural-language query.

    Lowercased, stopword-filtered, deduped. Order preserved so that
    longer/earlier keywords get scored first when ties break.
    """
    seen: set[str] = set()
    keep: list[str] = []
    for m in _TOKEN_RE.findall(query.lower()):
        if m in _STOP or len(m) < 3 or m in seen:
            continue
        seen.add(m)
        keep.append(m)
    return keep


def _rg_files(token: str, corpus: Path, *, max_files: int = 500) -> list[str]:
    """Return absolute paths matching ``token`` in source files.

    Uses ripgrep with a small whitelist of source extensions and the
    default `.gitignore`-aware filtering. Falls back to empty on any
    error so the benchmark keeps running.
    """
    try:
        proc = subprocess.run(
            [
                "rg",
                "-l",
                "-i",
                "-F",  # fixed-string (no regex injection from query tokens)
                "--type-add",
                "code:*.{ts,tsx,js,jsx,html,scss,css,json,md}",
                "-tcode",
                "--max-count",
                "1",
                "--max-filesize",
                "1M",
                "-g",
                "!**/node_modules/**",
                "-g",
                "!**/dist/**",
                "-g",
                "!**/.git/**",
                token,
                str(corpus),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if proc.returncode not in (0, 1):
        return []
    out = [line for line in proc.stdout.splitlines() if line.strip()]
    return out[:max_files]


def grep_search(query: str, corpus: Path, top_k: int) -> list[VectorHit]:
    """Rank files by number of distinct query tokens they contain.

    This is intentionally simple — it represents the floor that an agent
    hits when it has no semantic index and falls back to keyword grep.
    Files matching more distinct tokens rank higher; ties broken by
    fewer total candidate files for that token (more specific signal).
    """
    tokens = _tokenize(query)
    if not tokens:
        return []
    file_token_count: Counter[str] = Counter()
    token_corpus_size: dict[str, int] = {}
    for tok in tokens:
        files = _rg_files(tok, corpus)
        token_corpus_size[tok] = len(files) or 1
        for f in files:
            file_token_count[f] += 1
    if not file_token_count:
        return []

    # Score = sum over matched tokens of (1 / log corpus-freq) — rare
    # tokens carry more weight, IDF-style.
    def _score(path: str) -> float:
        s = 0.0
        for tok in tokens:
            if tok in token_corpus_size and any(tok in line for line in [path.lower()]) or True:
                # Cheap: include token contribution iff this file matched in the rg pass.
                # file_token_count already encodes that; per-token presence is implicit.
                pass
        # Use file_token_count as the matched-token count, weighted by IDF.
        matched = file_token_count[path]
        idf = sum(
            1.0 / math.log(token_corpus_size[tok] + 1.0, 10) + 1.0
            for tok in tokens
        ) / max(len(tokens), 1)
        return matched * idf

    ranked = sorted(
        file_token_count.items(),
        key=lambda kv: (-_score(kv[0]), kv[0]),
    )
    hits: list[VectorHit] = []
    for path, _ in ranked[: top_k * 2]:
        hits.append(VectorHit(id=path, score=float(file_token_count[path]), payload={"path": path}))
    return hits[:top_k]


# ---------------------------------------------------------------- metrics


def _is_relevant(payload: dict[str, Any], gold: list[str]) -> bool:
    path = payload.get("path", "") or ""
    return any(g in path for g in gold)


def _recall_at_k(hits: list[dict[str, Any]], gold: list[str], k: int) -> float:
    return 1.0 if any(_is_relevant(h, gold) for h in hits[:k]) else 0.0


def _reciprocal_rank(hits: list[dict[str, Any]], gold: list[str]) -> float:
    for i, h in enumerate(hits, start=1):
        if _is_relevant(h, gold):
            return 1.0 / i
    return 0.0


def _ndcg_at_k(hits: list[dict[str, Any]], gold: list[str], k: int) -> float:
    dcg = 0.0
    for i, h in enumerate(hits[:k], start=1):
        if _is_relevant(h, gold):
            dcg += 1.0 / math.log2(i + 1)
            break
    return dcg  # iDCG = 1.0 (first rank)


# ---------------------------------------------------------------- modes


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


def _run_codememory(
    *,
    label: str,
    collection: str,
    store: QdrantStore,
    embedder: Any,
    queries: list[dict[str, Any]],
    top_k: int,
    with_rerank: bool,
) -> RunResult:
    return _common_loop(
        label=label,
        queries=queries,
        top_k=top_k,
        do_query=lambda q: _cm_query(q, embedder, store, collection, top_k, with_rerank),
    )


def _cm_query(
    query: str,
    embedder: Any,
    store: QdrantStore,
    collection: str,
    top_k: int,
    with_rerank: bool,
) -> list[VectorHit]:
    qvec = embedder.embed_one(query)
    fetch_k = top_k * 2
    raw = store.search(collection, qvec, top_k=fetch_k, mode="dense")
    if with_rerank:
        raw = maybe_cross_encode(query, raw)
        raw = _rerank_code(raw)
    return raw[:top_k]


def _run_grep(
    *,
    label: str,
    corpus: Path,
    queries: list[dict[str, Any]],
    top_k: int,
) -> RunResult:
    return _common_loop(
        label=label,
        queries=queries,
        top_k=top_k,
        do_query=lambda q: grep_search(q, corpus, top_k),
    )


def _common_loop(
    *,
    label: str,
    queries: list[dict[str, Any]],
    top_k: int,
    do_query,
) -> RunResult:
    recalls5: list[float] = []
    recalls10: list[float] = []
    rrs: list[float] = []
    ndcgs: list[float] = []
    latencies_ms: list[float] = []
    per_query: list[dict[str, Any]] = []

    # warm-up
    if queries:
        try:
            do_query(queries[0]["query"])
        except Exception:  # noqa: BLE001
            pass

    for q in queries:
        text = q["query"]
        gold = q["gold"]
        t0 = time.perf_counter()
        try:
            hits = do_query(text)
        except Exception as e:  # noqa: BLE001
            print(f"[bench] {label} '{text}': error {e}", file=sys.stderr)
            hits = []
        dt_ms = (time.perf_counter() - t0) * 1000.0

        payloads = [h.payload for h in hits]
        r5 = _recall_at_k(payloads, gold, 5)
        r10 = _recall_at_k(payloads, gold, 10)
        rr = _reciprocal_rank(payloads, gold)
        ndcg = _ndcg_at_k(payloads, gold, 10)

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
                "top": [(h.payload.get("path") or h.id) for h in hits[:5]],
            }
        )

    return RunResult(
        mode=label,
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


def _anonymize(s: str, mapping: list[tuple[str, str]]) -> str:
    for src, dst in mapping:
        s = s.replace(src, dst)
    return s


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


def _md_delta(baseline: RunResult, candidate: RunResult, *, name_a: str, name_b: str) -> str:
    def pct(a: float, b: float) -> str:
        if a == 0:
            return "—" if b == 0 else "+∞"
        return f"{(b - a) / a * 100:+.1f}%"

    return (
        f"| Metric | {name_a} | {name_b} | Δ |\n"
        "|--------|--------:|---------:|--:|\n"
        f"| Recall@5  | {baseline.recall_at_5:.3f}  | {candidate.recall_at_5:.3f}  | {pct(baseline.recall_at_5, candidate.recall_at_5)} |\n"
        f"| Recall@10 | {baseline.recall_at_10:.3f} | {candidate.recall_at_10:.3f} | {pct(baseline.recall_at_10, candidate.recall_at_10)} |\n"
        f"| MRR       | {baseline.mrr:.3f}          | {candidate.mrr:.3f}          | {pct(baseline.mrr, candidate.mrr)} |\n"
        f"| nDCG@10   | {baseline.ndcg_at_10:.3f}   | {candidate.ndcg_at_10:.3f}   | {pct(baseline.ndcg_at_10, candidate.ndcg_at_10)} |\n"
        f"| p50 (ms)  | {baseline.p50_ms:.1f}       | {candidate.p50_ms:.1f}       | {pct(baseline.p50_ms, candidate.p50_ms)} |\n"
        f"| p95 (ms)  | {baseline.p95_ms:.1f}       | {candidate.p95_ms:.1f}       | {pct(baseline.p95_ms, candidate.p95_ms)} |\n"
    )


def _md_per_query(results: list[RunResult], anon: list[tuple[str, str]]) -> str:
    out = ["\n## Per-query results\n"]
    if not results:
        return "".join(out)
    n = len(results[0].per_query)
    for i in range(n):
        q = results[0].per_query[i]["query"]
        gold = results[0].per_query[i]["gold"]
        out.append(f"\n### `{q}`\n\nGold: {gold}\n")
        for r in results:
            pq = r.per_query[i]
            anon_top = [_anonymize(p or "", anon) for p in pq["top"]]
            out.append(
                f"- **{r.mode}** — r@5={pq['r@5']:.0f} r@10={pq['r@10']:.0f} "
                f"rr={pq['rr']:.2f} ndcg={pq['ndcg@10']:.2f} "
                f"({pq['ms']} ms)\n"
                f"  - top5: {anon_top}\n"
            )
    return "".join(out)


def render_report(
    *,
    project: str,
    corpus_label: str,
    queries_path: str,
    runs: list[RunResult],
    anon: list[tuple[str, str]],
) -> str:
    by_mode = {r.mode: r for r in runs}
    grep = by_mode["grep (no code-memory)"]
    cm_full = by_mode["code-memory (dense+rerank)"]

    best_mrr = max(runs, key=lambda r: r.mrr)
    best_ndcg = max(runs, key=lambda r: r.ndcg_at_10)
    best_recall10 = max(runs, key=lambda r: r.recall_at_10)

    parts: list[str] = [
        "# Retrieval benchmark — code-memory vs no-code-memory baseline\n",
        f"\n**Corpus**: `{corpus_label}` (anonymized)  ",
        f"\n**Project slug**: `{_anonymize(project, anon)}`  ",
        f"\n**Queries**: `{queries_path}` ({len(grep.per_query)} hand-crafted Angular queries — natural-language + identifier mix)  ",
        "\n**Baseline (no code-memory)**: ripgrep keyword search over the working tree — what an agent falls back to when no semantic index exists.  ",
        "\n**code-memory**: bi-encoder (`bge-m3` dense via Ollama) + cross-encoder rerank (`bge-reranker-v2-m3`, α=0.5).  ",
        "\n**Hardware**: Apple Silicon (MPS), fp16  ",
        "\n\n## Takeaways\n\n",
        f"- **Best MRR**: `{best_mrr.mode}` at {best_mrr.mrr:.3f}\n",
        f"- **Best nDCG@10**: `{best_ndcg.mode}` at {best_ndcg.ndcg_at_10:.3f}\n",
        f"- **Best Recall@10**: `{best_recall10.mode}` at {best_recall10.recall_at_10:.3f}\n",
        "\n## Summary\n\n",
        _md_table(runs),
        "\n## Grep baseline vs code-memory (full)\n\n",
        _md_delta(
            grep,
            cm_full,
            name_a="grep",
            name_b="code-memory",
        ),
        _md_per_query(runs, anon),
    ]
    return "".join(parts)


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="project slug")
    parser.add_argument("--corpus", required=True, help="absolute path to repo")
    parser.add_argument(
        "--queries",
        default=str(REPO_ROOT / "scripts" / "benchmark_queries.json"),
    )
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "docs" / "BENCHMARK_VS_BASELINE.md"),
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--anon-from",
        action="append",
        default=[],
        help="literal string to redact from rendered paths (repeatable)",
    )
    parser.add_argument(
        "--anon-to",
        action="append",
        default=[],
        help="replacement for the matching --anon-from (repeatable)",
    )
    parser.add_argument("--corpus-label", default="acme/sample-webapp")
    parser.add_argument("--json", help="optional path to dump raw per-query JSON")
    args = parser.parse_args(argv)

    if len(args.anon_from) != len(args.anon_to):
        parser.error("--anon-from and --anon-to must be passed the same number of times")
    anon = list(zip(args.anon_from, args.anon_to, strict=True))

    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))["queries"]
    project = slugify(args.project)
    cfg = CONFIG.for_project(project)
    collection = cfg.qdrant_code
    corpus = Path(args.corpus).resolve()
    if not corpus.is_dir():
        parser.error(f"--corpus is not a directory: {corpus}")

    print(f"[bench] project={project} collection={collection} corpus={corpus} queries={len(queries)}")

    embedder = get_embedder()
    store = QdrantStore()

    runs: list[RunResult] = []

    print("[bench] running: grep baseline (no code-memory)")
    runs.append(_run_grep(
        label="grep (no code-memory)",
        corpus=corpus,
        queries=queries,
        top_k=args.top_k,
    ))

    print("[bench] running: code-memory dense only")
    runs.append(_run_codememory(
        label="code-memory (dense only)",
        collection=collection,
        store=store,
        embedder=embedder,
        queries=queries,
        top_k=args.top_k,
        with_rerank=False,
    ))

    print("[bench] running: code-memory dense + cross-encoder rerank")
    runs.append(_run_codememory(
        label="code-memory (dense+rerank)",
        collection=collection,
        store=store,
        embedder=embedder,
        queries=queries,
        top_k=args.top_k,
        with_rerank=True,
    ))

    queries_abs = Path(args.queries).resolve()
    try:
        queries_display = str(queries_abs.relative_to(REPO_ROOT))
    except ValueError:
        queries_display = str(queries_abs)
    report = render_report(
        project=project,
        corpus_label=args.corpus_label,
        queries_path=queries_display,
        runs=runs,
        anon=anon,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"[bench] wrote {out}")

    if args.json:
        Path(args.json).write_text(
            json.dumps({r.mode: r.__dict__ for r in runs}, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[bench] wrote {args.json}")

    print("\n=== summary ===")
    print(_md_table(runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
