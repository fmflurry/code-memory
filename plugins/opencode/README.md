# @code-memory/opencode-plugin

OpenCode plugin that makes the [`code-memory`](../..) backend ambient:
auto-retrieves a **Context Pack** when the user asks a substantive code
question, and auto-learns by re-indexing files the agent writes / edits.

The plugin is layered on top of the existing `code-memory` MCP server, which
remains available for the agent to call manually (`codememory_retrieve`,
`codememory_record`, `codememory_reingest`).

## What it does

| Hook                                  | Behavior                                                       |
| ------------------------------------- | -------------------------------------------------------------- |
| `chat.message` (first message of a session) | Kicks off a background `code-memory ingest <cwd>` (git delta) to catch edits made outside OpenCode since the last session. |
| `chat.message`                        | Detects substantive code intent → `code-memory retrieve --json` → caches a Context Pack per session for orientation only (5 min TTL, 60 s dedup). Also clears the per-turn gate flag (see `tool.execute.before`). |
| `experimental.chat.system.transform`  | Appends a fresh Context Pack to the system prompt, with explicit affordances for the 5 topology MCP tools (callers / callees / importers / dependencies / definitions). If a gate nudge is pending from the previous turn (the agent ran a read/shell tool without first making an explicit code-memory MCP call), surfaces a one-shot reminder here. |
| `tool.execute.before` (`read`/`bash`/`grep`/`glob`) | First-tool gate: if no explicit `codememory_*` MCP call has fired this turn, logs a warning to the OpenCode log and queues a one-shot nudge to drop into the next turn's system prompt. Never blocks. |
| `tool.execute.after` (`write`/`edit`/`patch`) | (a) Fires `code-memory reingest <path>`. (b) Drops the session's cached Context Pack so the next prompt re-fetches. (c) Schedules a debounced `code-memory resolve` to re-point cross-file CALLS edges. |
| `tool.execute.after` (`codememory_*` MCP) | Marks the gate flag as satisfied so subsequent reads / shells in the same turn stay silent. Auto-retrieve Context Packs do not satisfy this gate. |
| `event` (`session.idle`)              | Records the session as an episode via `code-memory record`.    |

All backend calls are best-effort. If `code-memory` is not on PATH, every
hook degrades to a benign no-op — your OpenCode session is never blocked.

## Requirements

1. The `code-memory` CLI on PATH. Recommended installs:
   ```bash
   # one-time install (writes a binary you can call without uvx)
   pipx install git+https://github.com/fmflurry/code-memory
   #   or
   uv tool install git+https://github.com/fmflurry/code-memory
   ```
   If you only have `uvx`, point the plugin at the wrapper command instead
   (see "Custom binary" below).

2. Running infra: FalkorDB + Qdrant + Ollama with `bge-m3`. See the main
   [README](../../README.md#installation).

3. The repo must have been ingested at least once:
   ```bash
   code-memory ingest /path/to/repo
   ```

## Install

OpenCode auto-discovers plugins under `~/.config/opencode/plugins/` (global)
or `<project>/.opencode/plugins/` (project-local). The bundled installer
symlinks this plugin into either location:

```bash
# global (default) — pick this for everyday use
./plugins/opencode/install.sh

# project-local (only for the current repo)
cd /path/to/repo
~/Workspace/code-memory/plugins/opencode/install.sh --project

# custom directory
./plugins/opencode/install.sh --target /some/other/dir
```

The installer creates two symlinks:
- `code-memory.ts` → the plugin entry
- `code-memory-lib/` → the helper modules

OpenCode loads `.ts` files at the top level of the plugin dir directly via
Bun — no build step needed. The `code-memory-lib/` subdirectory is treated
as private support code (the same convention used by `worktree.ts` /
`worktree/` in many setups).

Restart OpenCode after install.

### Use alongside the MCP server (recommended)

The plugin handles the *automatic* path; the MCP server still exposes the
manual tools so the agent can call them when it judges retrieval / recording
useful on its own.

```jsonc
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
  },
  "plugin": [
    "{env:HOME}/Workspace/code-memory/plugins/opencode/src/code-memory.ts"
  ]
}
```

## Configuration

Today the plugin reads no config file. Knobs are inline constants in
`src/code-memory.ts`:

| Constant               | Default          | Purpose                                                       |
| ---------------------- | ---------------- | ------------------------------------------------------------- |
| `PACK_TTL_MS`          | 5 min            | How long a cached Context Pack stays injectable.              |
| `DEDUP_WINDOW_MS`      | 60 s             | Same-query suppression to avoid hammering the embedder.       |
| `RESOLVER_DEBOUNCE_MS` | 1.5 s            | Quiet period after the last write before the resolver re-runs.|
| `WRITE_TOOLS`          | write/edit/patch | Which tool names trigger auto-reingest + resolver scheduling. |

### Custom binary

If `code-memory` isn't on PATH (for example, you only have `uvx`), shim it:

```bash
# put this in ~/.local/bin/code-memory and chmod +x
exec uvx --from git+https://github.com/fmflurry/code-memory code-memory "$@"
```

## Bundled skill

`skills/code-memory/SKILL.md` documents the tools, the Context Pack format,
and when the agent should call retrieve / record / reingest manually.
Wire it into your skills config the same way you wire any other Anthropic
Agent Skill.

## Development

```bash
cd plugins/opencode
npm install
npm run typecheck    # tsc --noEmit
```

The plugin is a single TypeScript module loaded by OpenCode's Bun runtime;
there is no build step.

## Behavior contract

- A missing or broken backend never crashes the session.
- Trivial replies ("yes", "ok", "continue") do **not** trigger retrieval.
- A re-ingest after `write` is fire-and-forget; the agent's turn returns
  immediately.
- Session episodes are written only on `session.idle`, with the captured
  first user message and `git diff` as the patch.

## Technical details — keeping the index fresh

The whole product proposition collapses if the agent answers from a stale
graph. Code-memory has two completely different states to keep current:

- **Per-file state** — symbol definitions, imports, and call expressions for
  a single source file. Cheap to rebuild from one file.
- **Cross-file state** — the resolved CALLS edges that point a caller at the
  *real* defined Symbol node (instead of a placeholder). Touching one file
  can invalidate edges in many others, so a single-file re-ingest is not
  enough on its own.

Both must move forward together, and edits can come from places the plugin
can't see (vim, IDE saves, `git pull`, `git stash pop`). Below is the full
matrix of failure modes and what the plugin does about each.

| Failure mode                                              | Mitigation                                                                                                                                                                                       |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Agent rewrites a file → that file's symbols are stale.    | `tool.execute.after` fires `code-memory reingest <path>` — tree-sitter re-parses, the file's nodes + edges are dropped and re-inserted, and its Qdrant chunks are replaced.                      |
| Agent rewrites a file → callers in *other* files now point at deleted/renamed symbols. | After every write, the plugin schedules a debounced `code-memory resolve`. The resolver scans the whole graph and re-points placeholder `name::X` CALLS edges to the real Symbol nodes.            |
| Agent does a 20-file refactor in 2 seconds → resolver would run 20 times back-to-back. | Resolver scheduling is debounced by `RESOLVER_DEBOUNCE_MS` (1.5 s). A new write resets the timer, so a burst of edits collapses to exactly one resolver run after the dust settles.                |
| File changes outside OpenCode between sessions (vim, IDE, `git pull`, `git checkout`). | First `chat.message` of each session triggers a one-shot background `code-memory ingest <cwd>`. The ingest is git-aware and only re-walks files whose hash moved — and it re-runs the resolver. |
| Session-cached Context Pack still reflects pre-write state. | Any successful write also drops the per-session pack cache. The next prompt re-fetches against the just-updated index instead of replaying stale code hits.                                       |
| Backend (FalkorDB / Qdrant / Ollama) is down.             | All CLI calls are guarded by per-command timeouts and run fire-and-forget. Failure is logged to the OpenCode log channel and silently no-op'd; the agent's turn is never blocked.                |
| `code-memory` CLI is missing on PATH.                     | `createMemoryClient` detects this once at plugin init and short-circuits every method. The plugin remains loaded but inert.                                                                       |
| Agent never explicitly records what it did.               | `session.idle` fires `code-memory record` with the first user message + cumulative `git diff` as the patch (verdict left blank). Future sessions can recall the episode even without manual record. |

### What is **not** yet covered

- **File deletions via the `write` tool.** `reingest` on a missing path skips cleanly, but the previous File node + DEFINES edges + chunks linger until the next git-aware delta ingest evicts them.
- **Renames** look like delete + create to the plugin. The graph keeps the old node until the next delta ingest.
- **Bare external module imports** (`@scope/pkg`, `rxjs`). The resolver does not chase npm dependencies, so callers into external packages stay unresolvable. This is an architectural choice, not a freshness bug.
- **Pure reads.** If the agent only opens files, no hook fires. That's correct: nothing changed.

### Mental model

Think of the plugin as three concentric refresh loops, each cheaper and more frequent than the next:

```
session start         every write              every chat turn
     │                     │                         │
     ▼                     ▼                         ▼
delta-ingest        reingest + debounce          retrieve
+ resolver            resolver + pack            (no I/O if
(catches OOB         invalidation               dedup'd within
 edits)              (cross-file edge            60 s)
                      accuracy)
```

If any layer fails, the layer above eventually catches up. The system
is designed so that stale data is a temporary state, not a steady state.

## License

MIT — see [LICENSE](../../LICENSE).
