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

</div>

---

## What is this?

`code-memory` gives a coding agent (Claude Code, OpenCode, Cursor, your own harness) a memory it can actually use:

- **Structural memory** — a symbol graph of every file, function, import, and call (FalkorDB).
- **Semantic memory** — vector embeddings of every symbol, queryable by natural language (Qdrant + Ollama).
- **Episodic memory** — a task log of past prompts, plans, patches, and outcomes (SQLite + embedded recall).

It runs entirely on your machine. No OpenAI calls. No cloud. No vendor lock-in. Designed to be *boring infrastructure* you can wire into any harness via CLI, hooks, or MCP.

```
              query
                │
                ▼
        ┌───────────────┐
        │  Embed (bge-m3)│
        └───────┬───────┘
                │
   ┌────────────┴────────────┐
   ▼                         ▼
Qdrant                  FalkorDB
(semantic top-k)        (graph neighbors)
   │                         │
   └────────────┬────────────┘
                ▼
         Context Pack → Agent
```

---

## Requirements

| Component         | Minimum version | Notes                                                              |
| ----------------- | --------------- | ------------------------------------------------------------------ |
| **Python**        | 3.11            | Used to build the orchestrator, CLI, and extractor.                |
| **Docker**        | 20.x (Compose v2) | Runs FalkorDB and Qdrant locally.                                |
| **Ollama**        | latest          | Local embeddings backend.                                          |
| **Embedding model** | `bge-m3`      | Pulled via `ollama pull bge-m3`. Alternatives: `nomic-embed-text`. |
| **Disk**          | ~3 GB           | Ollama model (~1.2 GB) + Docker volumes + Python deps.             |
| **RAM**           | 8 GB+           | 16 GB+ recommended for large repos.                                |
| **OS**            | macOS / Linux / Windows (WSL2) | Tested on Apple Silicon (M-series) and Linux x86_64. |

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
code-memory ingest /path/to/repo
```

This walks the repo, extracts symbols / imports / calls with tree-sitter, writes them to the FalkorDB graph, and indexes each symbol snippet into Qdrant via `bge-m3`.

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

### Inspect the stores

| Store      | UI                                                  |
| ---------- | --------------------------------------------------- |
| FalkorDB   | http://localhost:3000  (graph browser, Cypher)      |
| Qdrant     | http://localhost:6333/dashboard                     |
| SQLite     | `sqlite3 ./data/episodic.db`                        |

---

## Project layout

```
src/code_memory/
├── embed/            # Ollama embeddings wrapper
├── vector/           # Qdrant store
├── graph/            # FalkorDB store
├── extractor/        # tree-sitter -> symbols / imports / calls
├── episodic/         # SQLite task log
├── orchestrator/     # ingest pipeline, retrieve, context pack
└── cli.py            # typer-based CLI entrypoint
```

---

## Roadmap

- [ ] Per-project namespacing (separate graphs/collections per repo)
- [ ] File-watcher daemon for live re-ingest
- [ ] MCP server (`memory.retrieve`, `memory.record`, `memory.reingest`)
- [ ] Cross-encoder rerank step
- [ ] Resolved call edges (bind `CALLS` targets to actual `Symbol` nodes)
- [ ] More languages (Rust, Go, Java, C#)
- [ ] Hook recipes for Claude Code, OpenCode, Cursor

---

## License

MIT — see [LICENSE](LICENSE).
