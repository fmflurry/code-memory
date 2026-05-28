# @code-memory/opencode-plugin

OpenCode plugin that makes the [`code-memory`](../..) backend ambient:
auto-learns by re-indexing files the agent writes / edits, nudges the
agent toward the index before it greps / reads the filesystem, and
records the session as an episode on idle.

The plugin is layered on top of the existing `code-memory` MCP server, which
remains available for the agent to call manually (`codememory_retrieve`,
`codememory_record`, `codememory_reingest`).

## What it does

| Hook                                  | Behavior                                                       |
| ------------------------------------- | -------------------------------------------------------------- |
| `chat.message` (first message of a session) | Kicks off a background `code-memory ingest <cwd>` (git delta) to catch edits made outside OpenCode since the last session. Also installs a launchd / systemd watcher so out-of-session edits keep the index fresh. |
| `chat.message`                        | Detects durable claim intent in the user message (preference / decision / rejection / ownership / location) and queues a one-shot nudge reminding the agent to call `codememory_assert_claim`. Clears the per-turn gate flag (see `tool.execute.before`). |
| `experimental.chat.system.transform`  | Drains pending gate nudges and claim nudges into the system prompt at the start of the next turn. |
| `tool.execute.before` (`read`/`bash`/`grep`/`glob`) | First-tool gate: if no explicit `codememory_*` MCP call has fired this turn, logs a warning and queues a one-shot nudge to drop into the next turn's system prompt. Never blocks. |
| `tool.execute.after` (`write`/`edit`/`patch`) | (a) Fires `code-memory reingest <path>`. (b) Schedules a debounced `code-memory resolve` to re-point cross-file CALLS edges. |
| `tool.execute.after` (`codememory_*` MCP) | Marks the gate flag as satisfied so subsequent reads / shells in the same turn stay silent. |
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
| `RESOLVER_DEBOUNCE_MS` | 1.5 s            | Quiet period after the last write before the resolver re-runs.|
| `WRITE_TOOLS`          | write/edit/patch | Which tool names trigger auto-reingest + resolver scheduling. |

### Custom binary

If `code-memory` isn't on PATH (for example, you only have `uvx`), shim it:

```bash
# put this in ~/.local/bin/code-memory and chmod +x
exec uvx --from git+https://github.com/fmflurry/code-memory code-memory "$@"
```

## Bundled skill

`skills/code-memory/SKILL.md` documents the tools and when the agent
should call retrieve / record / reingest manually. Wire it into your
skills config the same way you wire any other Anthropic Agent Skill.

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
| Backend (FalkorDB / Qdrant / Ollama) is down.             | All CLI calls are guarded by per-command timeouts and run fire-and-forget. Failure is logged to the OpenCode log channel and silently no-op'd; the agent's turn is never blocked.                |
| `code-memory` CLI is missing on PATH.                     | `createMemoryClient` detects this once at plugin init and short-circuits every method. The plugin remains loaded but inert.                                                                       |
| Agent never explicitly records what it did.               | `session.idle` fires `code-memory record` with the first user message + cumulative `git diff` as the patch (verdict left blank). Future sessions can recall the episode even without manual record. |

### What is **not** yet covered

- **File deletions via the `write` tool.** `reingest` on a missing path skips cleanly, but the previous File node + DEFINES edges + chunks linger until the next git-aware delta ingest evicts them.
- **Renames** look like delete + create to the plugin. The graph keeps the old node until the next delta ingest.
- **Bare external module imports** (`@scope/pkg`, `rxjs`). The resolver does not chase npm dependencies, so callers into external packages stay unresolvable. This is an architectural choice, not a freshness bug.
- **Pure reads.** If the agent only opens files, no hook fires. That's correct: nothing changed.

### Mental model

Two refresh loops keep the index honest; the agent retrieves on demand
via MCP when it needs context:

```
session start              every write
     │                          │
     ▼                          ▼
delta-ingest              reingest + debounced
+ resolver                resolver (cross-file
(catches OOB               edge accuracy)
 edits)
```

If either layer fails, the next one eventually catches up. Stale data is a
temporary state, not a steady state. Context-pack delivery is the agent's
job — call `codememory_retrieve` when orientation is needed.

## License

MIT — see [LICENSE](../../LICENSE).
