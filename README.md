<div align="center">

<img src="docs/hero.png" alt="code-memory — local-first memory layer for coding agents" width="900">

# code-memory

**A lightweight, local-first memory layer for coding agents.**

Structural symbol graph &nbsp;·&nbsp; semantic vector recall &nbsp;·&nbsp; episodic task log

---

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FalkorDB](https://img.shields.io/badge/graph-FalkorDB-FF2C2C)](https://www.falkordb.com/)
[![Qdrant](https://img.shields.io/badge/vector-Qdrant-DC382D)](https://qdrant.tech/)
[![Ollama](https://img.shields.io/badge/embeddings-Ollama-000000)](https://ollama.com/)
[![tree-sitter](https://img.shields.io/badge/parser-tree--sitter-228B22)](https://tree-sitter.github.io/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Jump to:** [Get it running](#installation) &nbsp;·&nbsp; [Plug it into your agent](#mcp-server)

</div>

---

> [!NOTE]
> **Using this with a coding agent?** My personal Claude Code / OpenCode harness — hooks, agents, MCP wiring, and the `code-memory` integration — lives at **[fmflurry/settings-opencode](https://github.com/fmflurry/settings-opencode)**. Drop-in reference for plugging this memory layer into a real agent setup.

---

## What is this?

`code-memory` gives a coding agent (Claude Code, OpenCode, Cursor, your own harness) a memory it can actually use:

- **Structural memory** — a symbol graph of every file, function, import, and call (FalkorDB).
- **Semantic memory** — vector embeddings of every symbol, queryable by natural language (Qdrant + Ollama).
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
   with `bge-m3`, and stores:
   - vectors in **Qdrant** (semantic recall)
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
| **Ollama**          | latest                         | Local embeddings backend.                                          |
| **Embedding model** | `bge-m3`                       | Pulled via `ollama pull bge-m3`. Alternatives: `nomic-embed-text`. |
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

### MCP server

`code-memory` ships an MCP (Model Context Protocol) server that exposes the
same primitives as native tools. The agent can call them in its normal
tool-selection loop — no shell parsing, no slash command.

Tools advertised:

| Tool                  | Purpose                                                      |
| --------------------- | ------------------------------------------------------------ |
| `codememory_retrieve` | Semantic + graph + episodic recall (returns a Context Pack). |
| `codememory_record`   | Log a finished task (prompt / plan / patch / verdict).       |
| `codememory_reingest` | Re-index a single file after edits.                          |

Transport: stdio. Script entrypoint: `code-memory-mcp`.

#### Recommended: `uvx` (no clone, no venv)

Requires [`uv`](https://docs.astral.sh/uv/) (`brew install uv` / `pipx install uv`).
`uvx` fetches, caches, and runs the package in an isolated environment.

##### Claude Code

```bash
claude mcp add code-memory \
  --scope user \
  --env CODE_MEMORY_PROJECT=auto \
  -- uvx --from git+https://github.com/fmflurry/code-memory code-memory-mcp
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
        "git+https://github.com/fmflurry/code-memory",
        "code-memory-mcp"
      ],
      "enabled": true,
      "environment": { "CODE_MEMORY_PROJECT": "auto" }
    }
  }
}
```

Pin a version by appending `@<tag-or-sha>` to the git URL, e.g.
`git+https://github.com/fmflurry/code-memory@v0.1.0`.

> Once the package is published to PyPI the `--from git+…` part drops:
> `command: ["uvx", "code-memory-mcp"]`.

The server resolves the project slug from the cwd by default (git toplevel
basename), so it Just Works across repos. Override per-call with the
`project` argument or globally via `CODE_MEMORY_PROJECT`.

#### Alternative: pipx (persistent install on `$PATH`)

```bash
pipx install git+https://github.com/fmflurry/code-memory
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
uvx --from git+https://github.com/fmflurry/code-memory code-memory-mcp
# speaks JSON-RPC on stdio; useful with `npx @modelcontextprotocol/inspector`
```

### Inspect the stores

| Store    | UI                                            |
| -------- | --------------------------------------------- |
| FalkorDB | http://localhost:3000 (graph browser, Cypher) |
| Qdrant   | http://localhost:6333/dashboard               |
| SQLite   | `sqlite3 ./data/episodic.db`                  |

---

## Project layout

```
src/code_memory/
├── embed/            # Ollama embeddings wrapper
├── vector/           # Qdrant store
├── graph/            # FalkorDB store
├── extractor/        # tree-sitter -> symbols / imports / calls
├── episodic/         # SQLite task log
├── orchestrator/     # ingest pipeline, retrieval, context pack
│   ├── pipeline.py       # ingest_repo / ingest_file / reingest_file
│   ├── retrieve.py       # Retriever + ContextPack rendering
│   ├── ingest_state.py   # per-repo last_sha checkpoint (SQLite)
│   └── git_delta.py      # git diff -> changed / deleted / dirty
├── mcp_server.py     # stdio MCP server (`code-memory-mcp`)
└── cli.py            # typer-based CLI entrypoint
```

---

## Roadmap

- [x] Per-project namespacing (separate graphs / collections per repo)
- [x] MCP server (`codememory_retrieve`, `codememory_record`, `codememory_reingest`)
- [x] Git-aware incremental ingest (delta against last ingested commit)
- [ ] File-watcher daemon for live re-ingest
- [ ] Branch-aware index (auto re-walk on branch change)
- [ ] Cross-encoder rerank step
- [ ] Resolved call edges (bind `CALLS` targets to actual `Symbol` nodes)
- [ ] More languages (Rust, Go, Java, C#)
- [ ] Hook recipes for Claude Code, OpenCode, Cursor
- [ ] PyPI release (drops the `--from git+…` from the `uvx` install)

---

## License

MIT — see [LICENSE](LICENSE).
