<div align="center">

<img src="docs/hero.png" alt="code-memory — local-first memory layer for coding agents" width="900">

# code-memory

**A lightweight, local-first memory layer for coding agents.**

Structural symbol graph &nbsp;·&nbsp; semantic vector recall &nbsp;·&nbsp; episodic task log

---

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FalkorDB](https://img.shields.io/badge/graph-FalkorDB-FF2C2C)](https://www.falkordb.com/)
[![Qdrant](https://img.shields.io/badge/vector-Qdrant-DC382D)](https://qdrant.tech/)
[![Ollama](https://img.shields.io/badge/embeddings-Ollama%20bge--m3-000000)](https://ollama.com/library/bge-m3)
[![tree-sitter](https://img.shields.io/badge/parser-tree--sitter-228B22)](https://tree-sitter.github.io/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Jump to:** [Get it running](#installation) &nbsp;·&nbsp; [Plug it into your agent](#mcp-server)

</div>

---

> [!NOTE]
> **Using this with a coding agent?** My personal Claude Code / OpenCode harness — hooks, agents, MCP wiring, and the `code-memory` integration — lives at **[dev/settings-opencode](https://github.com/dev/settings-opencode)**. Drop-in reference for plugging this memory layer into a real agent setup.

---

## What is this?

`code-memory` gives a coding agent (Claude Code, OpenCode, Cursor, your own harness) a memory it can actually use:

- **Structural memory** — a symbol graph of every file, function, import, and call (FalkorDB).
- **Semantic memory** — dense embeddings of every symbol (Qdrant + Ollama-served `bge-m3`), with an opt-in dense+sparse hybrid path for symbol-heavy corpora.
- **Episodic memory** — a task log of past prompts, plans, patches, and outcomes (SQLite + embedded recall).

It runs entirely on your machine. No OpenAI calls. No cloud. No vendor lock-in. Designed to be _boring infrastructure_ you can wire into any harness via CLI, hooks, or MCP.

<div align="center">
  <img src="docs/architecture.png" alt="code-memory architecture — agent → orchestrator → extractor / embedder / episodic → FalkorDB + Qdrant + SQLite" width="900">
</div>

---

## Why it doesn't blow up your context window

Naive "give the LLM your whole repo" approaches die fast: context windows are
finite, attention degrades with size, and tokens cost money. `code-memory`
sidesteps that by **keeping the bulk of the knowledge outside the prompt** and
injecting only a small, query-relevant slice when the agent actually needs it.

The trick is a two-phase split:

1. **Offline — index everything, inject nothing.**
   Ingest walks the repo once, chunks code with tree-sitter, embeds each chunk
   with `bge-m3` (default: served by **Ollama** so the model stays warm across
   per-file save-hook re-ingests; opt-in `EMBED_BACKEND=flagembed` for true
   dense+sparse), and stores:
   - vectors in **Qdrant** (semantic recall, hybrid-ready collection layout)
   - symbol / import / call edges in **FalkorDB** (structural recall)
   - past prompts / plans / patches / verdicts in **SQLite** (episodic recall)

   None of this lives in the LLM context. It lives on disk.

2. **Online — retrieve a small, focused Context Pack.**
   When the agent has a question, it sends a natural-language query. The
   retriever:
   - embeds the query and pulls top-k semantically similar chunks from Qdrant
   - expands the neighborhood in FalkorDB (callers, callees, imports, types)
   - pulls a few similar past episodes from SQLite
   - returns a compact, ranked **Context Pack** — typically a handful of
     snippets, not the whole repo

The agent only ever sees the Context Pack, not the index behind it. The repo
can be 5 MB or 500 MB — what hits the prompt is the same small budget of
relevant chunks.

```
500 MB repo on disk     ─┐
                         │   index (Qdrant + FalkorDB + SQLite)
                         ▼
                  ┌──────────────┐
   query  ──────► │  retriever   │ ──►  ~few KB Context Pack  ──►  LLM prompt
                  └──────────────┘
```

Net effect: memory grows with the repo; **context stays roughly constant**.

---

## Two-stage retrieval: bi-encoder + cross-encoder (+ hybrid opt-in)

Retrieval quality is the difference between an agent that hits the right file
on the first try and one that flounders for ten tool calls. `code-memory` runs
a **two-stage** pipeline with a third opt-in lexical layer:

### Stage 1 — `bge-m3` bi-encoder (always on, default path)

Every code chunk is embedded once with `bge-m3`. The default backend is
**Ollama** — the daemon keeps the model warm across short-lived processes,
which matters because the Claude Code / OpenCode plugins fire a per-save
`code-memory reingest <path>` after every `Write|Edit`. An in-process
embedder would pay a multi-second cold load on every save; Ollama costs ~0
extra startup.

The query is embedded the same way, and Qdrant returns the top-N chunks by
cosine similarity in milliseconds. Collection layout is named-vector (`dense`
+ `sparse` slots) so the door stays open for lexical fusion without
re-ingestion; the `sparse` slot is simply empty under the Ollama backend.

### Hybrid opt-in — `EMBED_BACKEND=flagembed` + `CODEMEMORY_HYBRID=1`

For corpora where the agent queries mostly by exact symbol name
(`JwtAuthGuard.canActivate`, `useCustomerStore`), you can flip to the
in-process FlagEmbedding backend, which emits **both** the dense vector
**and** a learned sparse lexical vector from one forward pass:

```bash
uv sync --extra hybrid
export EMBED_BACKEND=flagembed
export CODEMEMORY_HYBRID=1
# then re-ingest so the sparse slot gets populated
code-memory ingest . --full
```

Queries then go through Qdrant's server-side **Reciprocal Rank Fusion**:

```
RRF(d) = Σ_branches  1 / (k + rank_branch(d))
```

Why this is **opt-in, not default**: benchmarks on a 2.6k-file Angular
corpus (see [`docs/BENCHMARK.md`](docs/BENCHMARK.md)) showed pure dense
outperforming hybrid (RRF and DBSF) on realistic queries — m3's dense head
is strong enough that adding sparse tends to surface generated API stubs
and `.spec.ts` files that share identifier vocabulary with the query,
without bringing new wins. And the FlagEmbedding backend trades Ollama's
warm-daemon model for an in-process load (~5-15 s) which is a regression
for hook-driven workflows. Re-run `scripts/benchmark.py` against your own
corpus before flipping it.

### Stage 2 — cross-encoder rerank (auto on Metal/CUDA, blended)

The top-N candidates from stage 1 are then rescored by a **cross-encoder**
(`BAAI/bge-reranker-v2-m3` by default): query and chunk are concatenated and
fed through a transformer that produces a *joint* relevance score. Because
the model attends to both sides simultaneously, it picks up semantic
relationships a bi-encoder cosine sim misses. The tradeoff: one forward pass
per *pair*, so it only makes sense on the already-narrow candidate set —
never on the whole index.

The final score is a **blend**, not a replacement:

```
score = (1 - α) · bi_encoder_score + α · cross_encoder_score
```

with `α = 0.5` by default. We picked blending after A/B testing replace-mode
on a real Angular repo: the cross-encoder won on 2/5 queries (promoted the
actual token-manager service over generic error interceptors; promoted the
concrete `phone.validator` over a generic spec file), tied on 2/5, and
*regressed* on 1/5 (promoted a mock CORS file to #1 for "authentication
login flow"). Blending recovers from the regression while keeping the wins.

### Policy

- **`auto`** (default) — cross-encoder fires only when a Metal (Apple
  Silicon) or CUDA accelerator is detected. CPU-only hosts stay on bi-encoder
  alone — the latency hit isn't worth it.
- **`CODEMEMORY_RERANK=1`** — force on (even CPU).
- **`CODEMEMORY_RERANK=0`** — force off.
- **`CODEMEMORY_RERANK_ALPHA=0.7`** — push more weight to the cross-encoder
  (or `0.0` to fall back to pure bi-encoder without disabling the load).
- **`CODEMEMORY_RERANK_MODEL`** — swap the cross-encoder (default
  `BAAI/bge-reranker-v2-m3`).

Install the optional dependency once:

```bash
uv sync --extra rerank        # or: pip install -e .[rerank]
```

On a first call the model warms up (~2-5 s, ~1.1 GB from HF cache); after
that it's a singleton (~50-200 ms per query on MPS).

---

## Benchmarks

All benchmarks run on the same anonymized corpus — `acme/sample-webapp`,
~2.6k files, 5.5k chunks of an Angular clean-architecture frontend — and
the same 30 hand-crafted queries (`scripts/benchmark_queries.json`, mix of
natural-language and PascalCase identifier searches). Hardware: Apple
Silicon (MPS), fp16.

### Benchmark 1 — code-memory vs no-code-memory baseline

Does plugging code-memory into an agent actually help, vs the default
agent fallback (ripgrep keyword search)?

| Mode | Recall@10 | MRR | nDCG@10 | p50 (ms) |
|------|----------:|----:|--------:|---------:|
| grep (no code-memory) | 0.367 | 0.169 | 0.218 | 176 |
| **code-memory (dense)** | **0.967** | **0.798** | **0.840** | **86** |
| code-memory (dense + CE rerank) | 0.967 | 0.573 | 0.672 | 7855 |

**Headline:** Recall@10 **+164%**, MRR **+372%**, nDCG@10 **+285%** —
**and faster** than walking the filesystem (86 ms vs 176 ms) because
Qdrant ANN beats `rg -l` over thousands of files. The cross-encoder
regresses on this corpus; α=0.5 blending dampens but doesn't erase it.

Full per-query breakdown, top-5 paths, and latency p95 in
[`docs/BENCHMARK_VS_BASELINE.md`](docs/BENCHMARK_VS_BASELINE.md).
Regenerate against your own corpus:

```bash
uv run python scripts/benchmark_vs_baseline.py \
    --project <slug> --corpus /path/to/repo \
    --out docs/BENCHMARK_VS_BASELINE.md
```

### Benchmark 2 — dense vs hybrid vs cross-encoder rerank

Once you've decided to use code-memory, which retrieval mode wins?

| Mode | Recall@5 | MRR | nDCG@10 | p50 (ms) |
|------|---------:|----:|--------:|---------:|
| **dense (default)** | **0.967** | **0.798** | **0.840** | 36 |
| hybrid (`EMBED_BACKEND=flagembed` + `CODEMEMORY_HYBRID=1`) | 0.900 | 0.680 | 0.750 | 34 |
| dense + CE rerank | 0.900 | 0.568 | 0.668 | 8853 |
| hybrid + CE rerank | 0.933 | 0.787 | 0.831 | 10446 |

**Read it this way:** the m3 dense head is strong enough on its own that
adding the sparse view via RRF surfaces spec/generated noise without
clear wins. Cross-encoder rerank improves hybrid (closes the gap to
~0.787 MRR) but never beats pure dense, and costs ~200× the latency. We
ship **dense via Ollama as the default** and keep hybrid + rerank behind
opt-in env vars for corpora where the tradeoffs flip.

Full breakdown in [`docs/BENCHMARK.md`](docs/BENCHMARK.md). Regenerate:

```bash
uv run python scripts/benchmark.py --project <slug> --out docs/BENCHMARK.md
```

### Caveats

- These results reflect **one Angular codebase**. Symbol-heavy or
  polyglot corpora may favor hybrid; LLM-style narrative corpora may
  favor the cross-encoder. Always re-run on your own corpus before
  changing defaults.
- Gold paths are substring matches against `hit.payload.path`. Good for
  A/B comparison, not absolute IR.
- Rerank latency includes a cold model load on the first query; warm
  calls drop to ~250 ms per query but stay dominated by N pairs ×
  forward-pass cost.

---

## Auto-learning and auto-query

Without MCP, the agent has to call `code-memory retrieve` and `code-memory
record` explicitly (or via a hook). That works but pushes discipline onto
the user and the agent. The goal is to make memory **ambient**: the agent
uses it without thinking about it, and the memory updates itself as the
agent works.

This is what the **MCP server** unlocks.

### Auto-query

Exposed as an MCP server, `code-memory` advertises tools like:

- `memory.retrieve(query, k)` — semantic + graph recall
- `memory.neighbors(symbol)` — structural expansion around a symbol
- `memory.episodes(query)` — similar past tasks

The agent's normal tool-selection loop picks `memory.retrieve` the same way it
picks `Read` or `Grep` today — whenever the model judges that more context
would help. No slash command, no hook, no human in the loop. The user just
asks their question; the agent silently pulls the right chunks before
answering.

In practice this means:

- "How does the auth middleware work?" → agent calls `memory.retrieve("auth
middleware")` → gets the relevant 3-5 chunks → answers.
- "Refactor `UserService` to use the new repo pattern." → agent calls
  `memory.neighbors("UserService")` → sees every caller and dependency before
  touching code.
- "We had this bug last month, what did we do?" → agent calls
  `memory.episodes(...)` → recalls the prior fix.

### Auto-learning

The same MCP surface exposes write-side tools:

- `memory.record(prompt, plan, patch, verdict)` — log a finished task
- `memory.reingest(path)` — refresh the index for a changed file

Combined with editor / harness hooks (file save, commit, task completion),
this closes the loop:

- **File saved** → `memory.reingest(path)` keeps the index live, so retrieval
  never goes stale.
- **Task finished** → `memory.record(...)` writes the episode (what was
  asked, what was planned, what was patched, did it pass) into SQLite +
  Qdrant.
- **Next similar task** → that episode surfaces automatically via
  `memory.episodes(...)`.

The result is a memory that **gets smarter the more the agent uses the
codebase**, without ever bloating the context window. Each task teaches the
system; each query pays back only the few KB that are actually relevant.

Between sessions, `code-memory ingest` uses a **git delta** to catch up
anything the live hooks missed — see [Git-aware incremental
ingest](#git-aware-incremental-ingest) below.

> Status: an MCP server ships in the package (see [MCP server](#mcp-server)
> below). The same primitives remain reachable via the CLI (`retrieve`,
> `record`, `reingest`) for shell-hook workflows.

---

## Requirements

| Component           | Minimum version                | Notes                                                              |
| ------------------- | ------------------------------ | ------------------------------------------------------------------ |
| **Python**          | 3.11                           | Used to build the orchestrator, CLI, and extractor.                |
| **Docker**          | 20.x (Compose v2)              | Runs FalkorDB and Qdrant locally.                                  |
| **Ollama**          | latest                         | Default embedding backend (long-running daemon keeps `bge-m3` warm across CLI hooks). |
| **Embedding model** | `bge-m3`                       | `ollama pull bge-m3` (~1.2 GB). Optional opt-in: `EMBED_BACKEND=flagembed` for in-process dense+sparse via FlagEmbedding (~2.3 GB). |
| **Disk**            | ~3 GB                          | Ollama model (~1.2 GB) + Docker volumes + Python deps.             |
| **RAM**             | 8 GB+                          | 16 GB+ recommended for large repos.                                |
| **OS**              | macOS / Linux / Windows (WSL2) | Tested on Apple Silicon (M-series) and Linux x86_64.               |

### Optional

- **Redis CLI** (`brew install redis` / `apt install redis-tools`) for poking at FalkorDB from the terminal.
- **DB Browser for SQLite** (`brew install --cask db-browser-for-sqlite`) for browsing episodic memory.

---

## Installation

### One-shot scripts

We provide cross-platform install scripts under `scripts/`.

#### macOS / Linux

```bash
git clone https://github.com/<you>/code-memory.git
cd code-memory
./scripts/install.sh
```

#### Windows (PowerShell)

```powershell
git clone https://github.com/<you>/code-memory.git
cd code-memory
./scripts/install.ps1
```

Both scripts will:

1. Check prerequisites (`python3`, `docker`, `ollama`).
2. Create a Python virtual environment at `./.venv`.
3. Install the package in editable mode.
4. Copy `.env.example` → `.env` (if not present).
5. Start FalkorDB and Qdrant via Docker Compose.
6. Pull the `bge-m3` embedding model into Ollama.
7. Run the smoke test suite.
8. Optionally install the **OpenCode** and/or **Claude Code** harness plugins (see [Harness plugins](#harness-plugins)). Interactive by default; pass `--plugins=all|opencode|claudecode|none` to bypass the prompt.

```bash
# install everything plus both plugins (non-interactive)
./scripts/install.sh --plugins=all

# only the Claude Code plugin, project-local
./scripts/install.sh --plugins=claudecode --plugins-scope=project
```

---

### Manual installation

If you prefer to run each step by hand:

```bash
# 1. Clone the repo
git clone https://github.com/<you>/code-memory.git
cd code-memory

# 2. Start infrastructure (FalkorDB + Qdrant)
docker compose -f docker/docker-compose.yml up -d

# 3. Pull the embedding model
ollama pull bge-m3

# 4. Create Python environment
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 5. Configure environment
cp .env.example .env

# 6. Verify
pytest -q
```

You should now have a working `code-memory` CLI on your `$PATH` (inside the venv).

---

## Usage

### Ingest a repository

```bash
code-memory ingest /path/to/repo                 # auto: incremental if known, else full
code-memory ingest /path/to/repo --full          # force a complete re-walk
code-memory ingest /path/to/repo --since main    # diff main..HEAD
code-memory ingest /path/to/repo --dry-run       # show the plan, write nothing
```

This walks the repo, extracts symbols / imports / calls with tree-sitter, writes them to the FalkorDB graph, and indexes each symbol snippet into Qdrant via `bge-m3`.

The walker honors the repo's `.gitignore` (including nested ones) and skips
obvious generated junk — minified bundles (`*.min.js`, `*.min.css`),
sourcemaps, lockfiles, `node_modules`, build outputs — so embeddings aren't
burned on noise. Override the defaults via the project config if you need
to index something the walker normally drops.

#### Git-aware incremental ingest

After the first run, `code-memory` remembers the HEAD commit it last ingested
(per repository, in the project's SQLite). Subsequent `ingest` calls run
`git diff <last_sha>..HEAD --name-status -M` and only touch what actually
changed:

- **Added / Modified** files → re-extracted and re-embedded
- **Deleted** files → records dropped from FalkorDB + Qdrant
- **Renamed** files → old path purged, new path ingested
- **Dirty worktree** (uncommitted edits, including untracked) → also re-ingested

When the stored SHA is no longer reachable (history rewrite, branch deletion),
the next run automatically falls back to a full walk and re-records.
Non-git checkouts always do a full walk.

Check what's pending:

```bash
code-memory ingest-status /path/to/repo
# -> { "last_sha": "...", "head_sha": "...", "drift": {"changed": 7, "deleted": 1}, "dirty": 3 }
```

> [!WARNING]
> **The first ingest can take a while.** Wall time is dominated by embedding
> every symbol through Ollama (`bge-m3` on CPU is the bottleneck on most
> laptops) and scales roughly linearly with codebase size. Rough orders of
> magnitude on an M-series Mac:
>
> - Small repo (~1k symbols): seconds
> - Mid repo (~10k symbols): a few minutes
> - Large repo (100k+ symbols): tens of minutes to an hour+
>
> Subsequent runs use the git delta described above, so they finish in
> seconds even on large repos. GPU-accelerated Ollama, fewer files (tune
> ignore globs), or a smaller embedding model (`nomic-embed-text`) all cut
> the first-run cost too.

### Query the memory

```bash
code-memory retrieve "where is the auth middleware defined?"
```

Returns a structured **Context Pack**: top-k semantic hits, neighborhood expansion, and any similar past episodes.

### Re-ingest a single file (for live workflows / hooks)

```bash
code-memory reingest src/auth.ts
```

### Record a task episode

```bash
code-memory record \
  --prompt "fix the login timeout" \
  --plan "extend session, add retry" \
  --patch "$(git diff)" \
  --verdict pass
```

### Navigate the call / import graph

The graph store backs five topology commands. Each prints JSON (`--json`) or
a human table:

```bash
code-memory callers getBearerToken              # who calls this symbol?
code-memory callees UserService --depth 2       # what does UserService reach?
code-memory definitions UserService             # all defining files + lines
code-memory dependencies src/auth.ts            # what does this file import?
code-memory importers '@acme-ng/security'     # who imports this package?
code-memory resolve                             # rebuild call/import edges
```

These same five surfaces are exposed as MCP tools (`codememory_callers`,
`codememory_callees`, `codememory_definitions`, `codememory_dependencies`,
`codememory_importers`) so the agent can do impact analysis without shelling
out.

### Reset a project's index

```bash
code-memory reset                          # wipe current project (with confirm)
code-memory reset --yes                    # skip confirmation
code-memory reset --include-episodes       # also drop conversation history
code-memory reset --all                    # every known project
```

Default scope drops Qdrant vectors + FalkorDB graph + ingest_state for the
project; episodic memory is preserved unless `--include-episodes` is passed.
`code-memory ingest --full` triggers the same reset implicitly before
re-walking.

### MCP server

`code-memory` ships an MCP (Model Context Protocol) server that exposes the
same primitives as native tools. The agent can call them in its normal
tool-selection loop — no shell parsing, no slash command.

Tools advertised:

**Core**

| Tool                      | Purpose                                                                                          |
| ------------------------- | ------------------------------------------------------------------------------------------------ |
| `codememory_retrieve`     | Semantic + graph + episodic recall (returns a Context Pack).                                     |
| `codememory_record`       | Log a finished task (prompt / plan / patch / verdict).                                           |
| `codememory_reingest`     | Re-index a single file after edits.                                                              |
| `codememory_ingest`       | Full / incremental repo ingest (long-running; requires explicit `confirmed=true`).               |

**Topology (call/import graph)**

| Tool                      | Purpose                                                                                          |
| ------------------------- | ------------------------------------------------------------------------------------------------ |
| `codememory_callers`      | Files that call a symbol (impact analysis: "what breaks if I rename X?").                        |
| `codememory_callees`      | Symbols called from the file that defines a given symbol (outgoing dependencies).                |
| `codememory_definitions`  | All files + line ranges that define a given symbol name (disambiguate before callers/callees).   |
| `codememory_dependencies` | Modules imported by a file (forward import graph).                                               |
| `codememory_importers`    | Files that import a module / package / path (reverse import graph).                              |

**.NET assembly surface**

| Tool                          | Purpose                                                                                       |
| ----------------------------- | --------------------------------------------------------------------------------------------- |
| `codememory_assembly_members` | List public methods of an indexed .NET Type, read on-demand from the referenced DLL (~tens of ms). Members aren't bulk-indexed to keep the graph small. |

**Temporal (time-travel queries)**

| Tool                            | Purpose                                                                                       |
| ------------------------------- | --------------------------------------------------------------------------------------------- |
| `codememory_drift`              | Symbols whose `last_seen_sha` ≠ supplied HEAD — classified `tombstoned` / `drifted`.          |
| `codememory_at_sha`             | Nodes (Symbol or File) alive at a past commit. Caller passes `sha_ord` from `git rev-list --count --first-parent <sha>`. |
| `codememory_callers_at_sha`     | Callers of a symbol as the graph looked at a past commit ("what called X before commit Y deleted it"). |

Every tool requires an explicit `project` argument — the silent cwd-fallback
was hiding namespace bugs (see commit `3663772`). Pass the slug printed by
`code-memory project` (or `code-memory projects` to list all known slugs).
Sentinel values like `auto` / `default` are rejected.

Transport: stdio. Script entrypoint: `code-memory-mcp`.

#### Recommended: `uvx` (no clone, no venv)

Requires [`uv`](https://docs.astral.sh/uv/) (`brew install uv` / `pipx install uv`).
`uvx` fetches, caches, and runs the package in an isolated environment.

##### Claude Code

```bash
claude mcp add code-memory \
  --scope user \
  -- uvx --from git+https://github.com/dev/code-memory code-memory-mcp
```

##### OpenCode

```json
{
  "mcp": {
    "code-memory": {
      "type": "local",
      "command": [
        "uvx",
        "--from",
        "git+https://github.com/dev/code-memory",
        "code-memory-mcp"
      ],
      "enabled": true
    }
  }
}
```

Pin a version by appending `@<tag-or-sha>` to the git URL, e.g.
`git+https://github.com/dev/code-memory@v0.1.0`.

> Once the package is published to PyPI the `--from git+…` part drops:
> `command: ["uvx", "code-memory-mcp"]`.

Every tool call must include the `project` slug — the server no longer
falls back to cwd-detection. The startup-detected slug is surfaced in each
tool's schema description so the agent has the exact value to pass; the
harness plugins (see [Harness plugins](#harness-plugins)) wire this
automatically.

#### Alternative: pipx (persistent install on `$PATH`)

```bash
pipx install git+https://github.com/dev/code-memory
# then in any MCP client:
#   command: ["code-memory-mcp"]
```

#### Local checkout (development)

```json
{
  "mcp": {
    "code-memory": {
      "type": "local",
      "command": ["/absolute/path/to/code-memory/.venv/bin/code-memory-mcp"],
      "enabled": true,
      "environment": { "CODE_MEMORY_PROJECT": "auto" }
    }
  }
}
```

#### Run directly (debug)

```bash
uvx --from git+https://github.com/dev/code-memory code-memory-mcp
# speaks JSON-RPC on stdio; useful with `npx @modelcontextprotocol/inspector`
```

### Harness plugins

The MCP server above exposes manual tools. The **harness plugins** make
the same backend **ambient** — auto-retrieving a Context Pack on every
substantive user prompt, auto-reingesting on every `Write` / `Edit`, and
recording sessions as episodes when the agent stops. The plugins are
optional and live alongside the MCP server; install both for the best
experience.

| Plugin                                          | Hook model                                                                    |
| ----------------------------------------------- | ----------------------------------------------------------------------------- |
| [`plugins/opencode`](plugins/opencode/README.md)    | Bun-loaded TypeScript module; uses `chat.message`, `experimental.chat.system.transform`, `tool.execute.after`, `session.idle`. |
| [`plugins/claude-code`](plugins/claude-code/README.md) | Plain Node scripts wired via `hooks.json`; uses `SessionStart`, `UserPromptSubmit`, `PostToolUse` (`Write`/`Edit`/`MultiEdit`), `Stop`. |

Both plugins:

- Detect substantive code intent (trivial follow-ups skip retrieval).
- Dedup the same query within 60 s.
- Debounce the cross-file resolver (~1.5 s after the last write) so a
  20-file refactor collapses to exactly one resolver run.
- Run a one-shot git-delta ingest at session start to catch
  out-of-band edits (vim, IDE, `git pull`).
- Record the session as an episode on idle / stop with the first user
  message + `git diff` as the patch.

Install via the top-level installer:

```bash
./scripts/install.sh --plugins=all                       # both, global
./scripts/install.sh --plugins=claudecode                # Claude Code only
./scripts/install.sh --plugins=opencode,claudecode       # both, csv form
./scripts/install.sh --plugins=all --plugins-scope=project   # ./.opencode and ./.claude
```

Or install a single plugin directly without re-running the backend
installer:

```bash
./plugins/opencode/install.sh        # ~/.config/opencode/plugins/
./plugins/claude-code/install.sh     # ~/.claude/plugins/code-memory/
```

Restart your agent after install.

### Inspect the stores

| Store    | UI                                            |
| -------- | --------------------------------------------- |
| FalkorDB | http://localhost:3000 (graph browser, Cypher) |
| Qdrant   | http://localhost:6333/dashboard               |
| SQLite   | `sqlite3 ./data/episodic.db`                  |

---

## Team sync — share memory across developers

A single dev's full ingest is expensive. The team-sync layer makes that work shareable
so dev B inherits dev A's index without re-running ingest, then keeps the index in
lockstep with the code as branches move.

### How it works

```
┌─────────────────────────────┐
│  main branch (your code)    │
├─────────────────────────────┤
│  feat/x, feat/y, ...        │
├─────────────────────────────┤
│  codemem-snapshots (orphan) │ ← per-commit snapshots, content-addressed
│   ├─ snapshots/<sha>.cmsnap │
│   ├─ manifests/<sha>.json   │
│   └─ index.json             │
└─────────────────────────────┘
```

A **snapshot** is a tar.gz containing the full vectors + graph + state for a single
git SHA, plus a manifest with `embed_model`, `embed_dim`, and a `sha256` content
digest. Snapshots live on an **orphan git branch** named `codemem-snapshots` —
no external storage, no CI, just `git push`/`git fetch`.

Snapshots are **content-addressed** by the SHA they represent. Two devs publishing
for the same SHA produce identical bytes (deterministic dump + canonical sort) so
concurrent publishes converge instead of conflicting.

### Trust guarantees

| Property            | Mechanism                                                    |
| ------------------- | ------------------------------------------------------------ |
| Reproducible        | snapshot keyed by `git SHA` + `embed_model`                  |
| Tamper-evident      | `manifest.content_sha256` recomputed on every load           |
| Model-drift safe    | `verify_snapshot()` rejects mismatched `embed_model` / `dim` |
| Disaster recovery   | any dev can re-publish; no single point of failure           |
| Offline-safe        | local incremental works without `git fetch`                  |

### Concrete workflow

#### Dev A merges to main, publishes a snapshot

```bash
# after merging PR
git checkout main && git pull
code-memory sync . --publish
# -> ingests latest main, then pushes <sha>.cmsnap to codemem-snapshots
```

Or one-shot:

```bash
code-memory snapshot publish .
```

#### Dev B clones fresh — instant memory, no ingest

```bash
git clone <repo> && cd <repo>
code-memory hooks install        # one-time setup; installs hooks + autostart
code-memory sync                 # pulls snapshot for HEAD, applies it
```

Output:

```json
{ "action": "pull_snapshot", "head_sha": "abc123...", "snapshot_sha": "abc123..." }
```

No tree-sitter walk. No embedding calls. Index ready in seconds.

#### Dev B pulls main with new commits

When git hooks are installed, `git pull` automatically triggers `code-memory sync`:

```
$ git pull
...
[sync] HEAD = ghi789
[sync] snapshot ghi789 found in codemem-snapshots
[sync] action=pull_snapshot
```

If no snapshot exists for that exact SHA, the syncer walks back via `rev-list
--first-parent` to find the nearest ancestor snapshot and applies it, then runs
an incremental ingest for the commits in between.

#### Dev B on a feature branch

```bash
git checkout -b feat/y         # post-checkout hook fires
# -> action=pull_then_incremental (snapshot=abc123, base=abc123)
# edits files...
# watcher (auto-running) detects edits, debounces 2s, runs incremental
```

Feature branches never publish (only commits on `--canonical-branch main`).

### Automated sync — four layers of resilience

```
┌──────────────────────────┐
│ 1. Git hooks             │ ← post-checkout, post-merge, post-rewrite,
│                          │   post-commit, post-applypatch
│                          │   (run code-memory sync in background)
├──────────────────────────┤
│ 2. Filesystem watcher    │ ← watchdog (FSEvents/inotify/ReadDirChangesW),
│                          │   debounce 2s, catches saves between commits
├──────────────────────────┤
│ 3. MCP pre-query guard   │ ← every codememory_retrieve checks HEAD drift,
│                          │   syncs if stale (cheap noop when clean)
├──────────────────────────┤
│ 4. OS autostart service  │ ← runs watcher independent of MCP host;
│                          │   launchd / systemd --user / schtasks
└──────────────────────────┘
```

Each layer is a safety net for the others. Hooks bypassed? Watcher catches it.
Watcher crashes? Autostart restarts. Autostart blocked? MCP guard catches drift
on the next query.

### Cross-platform autostart

The OS service runs `code-memory watch <repo>` at every user logon. Zero admin
required, fully user-level:

| OS      | Mechanism             | Unit location                                              |
| ------- | --------------------- | ---------------------------------------------------------- |
| macOS   | launchd LaunchAgent   | `~/Library/LaunchAgents/com.codememory.watch.<slug>.plist` |
| Linux   | systemd --user        | `~/.config/systemd/user/codememory-watch-<slug>.service`   |
| Windows | Task Scheduler (logon)| `\CodeMemory\Watch\<slug>` (task name)                     |

Bootstrap is **automatic** — the MCP server registers and starts the service the
first time it's invoked in a repo. No manual install step. The `Watcher` class
also runs in-process inside the MCP server as a belt-and-suspenders fallback for
sessions where OS autostart is unavailable (corporate-locked machines, CI containers).

### CLI surface

```bash
code-memory sync [ROOT]              # smart reconcile: pull / incremental / full
code-memory status [ROOT]            # unified view: autostart, hooks, snapshot, drift
code-memory watch [ROOT]             # foreground watcher (for tmux / screen)
code-memory hooks install            # git hooks + autostart (idempotent)
code-memory hooks uninstall          # clean removal
code-memory snapshot publish [ROOT]  # build snapshot for HEAD, push to branch
code-memory snapshot list            # list snapshots on the snapshot branch
code-memory snapshot gc --keep 20    # prune all but recent N snapshots
code-memory autostart status         # OS service health
```

### Decision tree (what `sync` actually does)

| State                                            | Action                  |
| ------------------------------------------------ | ----------------------- |
| HEAD == state.sha, worktree clean                | `noop`                  |
| HEAD == state.sha, worktree dirty                | `dirty_only` (reingest dirty files) |
| No local state, snapshot exists for HEAD         | `pull_snapshot`         |
| No local state, snapshot exists for ancestor     | `pull_then_incremental` |
| No local state, no snapshot                      | `full_ingest`           |
| HEAD moved, snapshot exists for HEAD             | `pull_snapshot`         |
| HEAD moved, snapshot for ancestor                | `pull_then_incremental` |
| HEAD moved, state.sha reachable                  | `incremental`           |
| HEAD moved, state.sha rewritten                  | `full_ingest`           |

### Environment variables

| Var                              | Effect                                                          |
| -------------------------------- | --------------------------------------------------------------- |
| `CODE_MEMORY_REPO`               | Override repo root for MCP bootstrap (default: cwd / git top)   |
| `CODE_MEMORY_NO_AUTOSTART`       | Skip OS autostart registration on MCP boot                      |
| `CODE_MEMORY_NO_BOOT_SYNC`       | Skip initial sync on MCP boot                                   |
| `CODE_MEMORY_NO_INPROC_WATCHER`  | Skip in-process watcher (use OS service only)                   |
| `CODE_MEMORY_NO_GUARD`           | Skip pre-query freshness guard                                  |
| `CODE_MEMORY_LOG_LEVEL`          | `DEBUG` / `INFO` / `WARNING` (default `INFO`)                   |
| `CODEMEMORY_RERANK`              | `auto` (default — on if Metal/CUDA), `1` (force on), `0` (force off) |
| `CODEMEMORY_RERANK_MODEL`        | Cross-encoder model id (default `BAAI/bge-reranker-v2-m3`)      |
| `CODEMEMORY_RERANK_ALPHA`        | Blend weight: `score = (1-α)·bi + α·ce` (default `0.5`)         |

### Failure modes & recovery

| Symptom                              | Resolution                                            |
| ------------------------------------ | ----------------------------------------------------- |
| `embed_model mismatch` on apply      | Re-run `code-memory ingest --full` and re-publish     |
| `content digest mismatch`            | Snapshot corrupt; `code-memory sync` falls back to incremental |
| Watcher not picking up changes       | `code-memory status` → check `running: true`; reinstall via `code-memory hooks install` |
| Snapshot branch grew too large       | `code-memory snapshot gc --keep 20`                   |
| Hook never fires after `git pull`    | Repo has `core.hooksPath` set elsewhere; the installer follows that path — verify with `git config core.hooksPath` |

### What is *not* shared

| Stored locally only                       | Why                                              |
| ----------------------------------------- | ------------------------------------------------ |
| Episodic memory (`episodic.db`)           | Personal: your task history, plans, verdicts     |
| Per-repo `ingest_state` SQLite row        | Records the dev's local checkpoint               |
| Working-tree-dirty incremental updates    | Uncommitted code shouldn't pollute team snapshot |

Snapshots only carry **vectors + graph + state for committed code**, scoped to a
project slug. Two projects in the same Qdrant/Falkor instance can publish
independently without colliding.

---

## Project layout

```
src/code_memory/
├── embed/            # Ollama embeddings wrapper
├── vector/           # Qdrant store
├── graph/            # FalkorDB store (callers / callees / definitions / imports)
├── extractor/        # tree-sitter -> symbols / imports / calls
│   └── gitignore.py      # .gitignore + minified/generated skip rules
├── episodic/         # SQLite task log
├── orchestrator/     # ingest pipeline, retrieval, context pack
│   ├── pipeline.py       # ingest_repo / ingest_file / reingest_file
│   ├── retrieve.py       # Retriever + ContextPack rendering (rerank, episode filter)
│   ├── resolver.py       # bind raw CALLS / IMPORTS to actual Symbol / File nodes
│   ├── reset.py          # wipe vectors + graph + ingest_state per project
│   ├── ingest_state.py   # per-repo last_sha checkpoint (SQLite)
│   └── git_delta.py      # git diff -> changed / deleted / dirty
├── sync/             # team-shared memory (snapshots, watcher, autostart)
│   ├── snapshot.py       # tar.gz blob: build / verify / apply
│   ├── store.py          # orphan-branch git-backed snapshot storage
│   ├── sync.py           # decision tree: pull / incremental / full
│   ├── watcher.py        # cross-platform filesystem watcher (watchdog)
│   ├── hooks.py          # git hooks installer (idempotent marker blocks)
│   └── autostart/        # OS service adapters
│       ├── launchd.py        # macOS LaunchAgent
│       ├── systemd.py        # Linux systemd --user
│       └── schtasks.py       # Windows Task Scheduler
├── mcp_server.py     # stdio MCP server (`code-memory-mcp`)
└── cli.py            # typer-based CLI entrypoint
```

---

## Temporal model — time-aware graph queries

The code graph is **bi-temporal at the SHA level**: every File, Symbol,
and edge carries three lifecycle properties stamped at ingest time.

| property | written when | answers |
|---|---|---|
| `first_seen_sha` | element is upserted for the first time | "when did this enter the codebase?" |
| `last_seen_sha`  | every subsequent re-ingest of the same element | "is this still alive at HEAD?" |
| `invalid_sha`    | element was deleted (file removed, symbol disappeared) | "when did this leave?" |

Deletions don't `DETACH DELETE` anymore — they **tombstone**. The node
stays in the graph with `invalid_sha = <removal commit>`. Topology
queries (`callers`, `callees`, `definitions`, `dependencies`,
`importers`) filter tombstones out by default, so the live view is
unchanged. The history stays queryable.

Each lifecycle stamp pairs a SHA string with a **topological ordinal**
(`first_seen_ord`, `last_seen_ord`, `invalid_ord`) computed via
`git rev-list --count --first-parent <sha>`. The ordinal is a monotonic
integer along the main branch — parent < child, always — which makes
range comparisons across SHAs ("alive *before* SHA X") cheap. Tombstones
additionally carry an `invalid_at` wall-clock timestamp.

### What this unlocks

```python
# Drift detection: symbols the latest ingest didn't confirm
store.drift(head_sha=current_head)
# -> [{'name': 'OldHelper', 'status': 'tombstoned', 'invalid_sha': '…'},
#     {'name': 'Renamed',  'status': 'drifted',    'last_seen_sha': '…'}]

# Time-travel: graph as of a past commit
store.at_sha(sha="abc123…", sha_ord=4271, label="Symbol")

# Who used to call this symbol before it was deleted?
store.callers_at_sha("getBearerToken", sha="abc123…", sha_ord=4271)
```

```cypher
// Pre-deletion callers of a now-removed symbol (without ordinals)
MATCH (caller)-[c:CALLS]->(s:Symbol {name: 'getBearerToken'})
WHERE s.invalid_sha = $deletion_sha
RETURN caller.key
```

### CLI

```bash
# Sanity-check a long-running watcher
code-memory drift -p my-project

# Bound monotonic graph growth
code-memory vacuum --before release/26.18      # by git ref
code-memory vacuum --older-than 30d            # by wall-clock age
code-memory vacuum --all --dry-run             # report counts without writing
```

### MCP tools

Agents reach the same surface via three native tools:

| tool | what |
|---|---|
| `codememory_drift(head_sha, project)` | symbols not last-seen at HEAD, classified `tombstoned` / `drifted` |
| `codememory_at_sha(sha, sha_ord, label?, limit?, project)` | nodes alive at a past commit (caller computes `sha_ord` with `git rev-list --count --first-parent <sha>`) |
| `codememory_callers_at_sha(symbol, sha, sha_ord, project)` | callers as the graph looked at that commit |

### Episode replay

`Episode.head_sha` records the git HEAD when the agent recorded the
episode. Combined with the graph stamps, you can reconstruct "what the
agent saw" without rewinding the working tree.

### What this doesn't do (yet)

- **Edge versioning is coarse.** An edge that disappears and reappears
  collapses into one row whose `last_seen_sha` jumps over the gap.
  Enough for "alive at SHA X" queries; not enough for "every commit
  that toggled this edge".
- **Vacuum is destructive and explicit.** Once you `vacuum --before X`,
  the pre-X tombstones are gone — `at_sha` / `callers_at_sha` for SHAs
  before X stop returning the dropped rows. Tradeoff is intentional;
  the design favours bounded growth over unbounded history.

See `CHANGELOG.md` for the design rationale.

## Roadmap

- [x] Per-project namespacing (separate graphs / collections per repo)
- [x] MCP server (retrieve / record / reingest / ingest + 5 graph tools)
- [x] Git-aware incremental ingest (delta against last ingested commit)
- [x] `.gitignore`-aware walker that skips minified bundles + generated junk
- [x] Resolved call / import edges (bind `CALLS` / `IMPORTS` to real nodes)
- [x] Lightweight rerank (entrypoint / generated boost, idle-episode filter)
- [x] Harness plugins for OpenCode and Claude Code (auto-retrieve + auto-learn)
- [x] `code-memory reset` CLI + auto-purge on `ingest --full`
- [x] File-watcher daemon for live re-ingest (cross-platform via `watchdog`)
- [x] Team-shared snapshots (orphan branch, content-addressed, model-aware verify)
- [x] OS autostart adapters (launchd / systemd / schtasks) — zero manual install
- [x] Branch-aware index (auto re-walk on branch change)
- [x] Cross-encoder rerank step (auto on Metal/CUDA; opt-in via `pip install code-memory[rerank]`)
- [x] Temporal stamping (`first_seen_sha` / `last_seen_sha` / `invalid_sha`) — see [Temporal model](#temporal-model--time-aware-graph-queries)
- [x] Topological SHA ordinals + `at_sha` / `callers_at_sha` time-travel queries
- [x] `code-memory vacuum --before <sha>` / `--older-than` / `--all` (tombstone GC)
- [x] MCP tools: `codememory_drift`, `codememory_at_sha`, `codememory_callers_at_sha`
- [x] .NET ecosystem (C#, Razor, VB.NET, F#)
- [ ] More languages (Rust, Go, Java)
- [ ] Cursor hook recipe
- [ ] PyPI release (drops the `--from git+…` from the `uvx` install)

---

## License

MIT — see [LICENSE](LICENSE).
