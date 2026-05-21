# @code-memory/claude-code-plugin

Claude Code plugin that makes the [`code-memory`](../..) backend ambient
inside Claude Code — same spirit as the
[OpenCode plugin](../opencode/README.md), adapted to Claude Code's hook
model.

It auto-retrieves a **Context Pack** when the user asks a substantive
code question, and auto-learns by re-indexing files the agent
writes / edits. The plugin sits next to the existing `code-memory` MCP
server, which stays available for the agent to call manually
(`codememory_retrieve`, `codememory_record`, `codememory_reingest`,
`codememory_callers`, …).

## What it does

| Hook             | Behavior                                                                                                                                                                                              |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SessionStart`   | Background `code-memory ingest <cwd>` (git delta) so the index reflects out-of-band edits (vim, IDE, `git pull`, `git checkout`) since the last session.                                              |
| `UserPromptSubmit` | Detects substantive code intent → `code-memory retrieve --json` → emits the formatted Context Pack as `additionalContext` so it lands in the model's view of this turn. 5 min cache, 60 s dedup.    |
| `PostToolUse` (`Write`/`Edit`/`MultiEdit`) | (a) Fires `code-memory reingest <path>`. (b) Invalidates the per-session Context Pack so the next prompt re-fetches. (c) Schedules a debounced `code-memory resolve` to re-point cross-file CALLS edges. |
| `Stop`           | Records the session as an episode via `code-memory record` with the first user message + `git diff` as the patch (best-effort).                                                                       |

All backend calls are best-effort. If `code-memory` is not on PATH,
every hook degrades to a benign no-op — your Claude Code session is
never blocked.

## Requirements

1. The `code-memory` CLI on PATH:

   ```bash
   pipx install git+https://github.com/fmflurry/code-memory
   #   or
   uv tool install git+https://github.com/fmflurry/code-memory
   ```

   If you only have `uvx`, shim it (see "Custom binary" below).

2. Running infra: FalkorDB + Qdrant + Ollama with `bge-m3`. See the main
   [README](../../README.md#installation).

3. The repo must have been ingested at least once:
   ```bash
   code-memory ingest /path/to/repo
   ```

4. Node.js 18+ on PATH (Claude Code already requires it).

## Install

```bash
# global (default) — pick this for everyday use
./plugins/claude-code/install.sh

# project-local
cd /path/to/repo
~/Workspace/code-memory/plugins/claude-code/install.sh --project

# custom directory
./plugins/claude-code/install.sh --target /some/other/dir
```

The installer symlinks the entire plugin directory into
`~/.claude/plugins/code-memory/` (or `<cwd>/.claude/plugins/code-memory/`
with `--project`). Claude Code discovers it on next start.

### Use alongside the MCP server (recommended)

The plugin handles the *automatic* path; the MCP server still exposes
the manual tools so the agent can call them when it judges retrieval /
recording useful on its own. Add to `~/.claude.json`:

```jsonc
{
  "mcpServers": {
    "code-memory": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/fmflurry/code-memory",
        "code-memory-mcp"
      ],
      "env": { "CODE_MEMORY_PROJECT": "auto" }
    }
  }
}
```

Restart Claude Code.

## Configuration

The plugin reads two environment variables:

| Variable               | Default              | Purpose                                                              |
| ---------------------- | -------------------- | -------------------------------------------------------------------- |
| `CODE_MEMORY_BIN`      | `code-memory`        | Override the binary name / absolute path.                            |
| `CODE_MEMORY_PROJECT`  | (none)               | Forwarded as `--project <slug>` to every CLI call. Useful for monorepos with non-standard slugs. |

Everything else lives as inline constants in the script files:

| Constant                                          | File                          | Default | Purpose                                                       |
| ------------------------------------------------- | ----------------------------- | ------- | ------------------------------------------------------------- |
| `DEDUP_WINDOW_MS`                                 | `scripts/on-user-prompt.js`   | 60 s    | Same-query suppression to avoid hammering the embedder.       |
| `RESOLVER_DEBOUNCE_MS`                            | `scripts/resolver-debounce.js`| 1.5 s   | Quiet period after the last write before the resolver re-runs.|
| Hook timeouts (`SessionStart`/`PostToolUse`/...)  | `hooks/hooks.json`            | 5–12 s  | Maximum wall-clock per hook invocation.                       |

### Custom binary

If `code-memory` isn't on PATH (for example, you only have `uvx`), shim
it:

```bash
# put this in ~/.local/bin/code-memory and chmod +x
exec uvx --from git+https://github.com/fmflurry/code-memory code-memory "$@"
```

Or point the plugin at the wrapper directly:

```bash
CODE_MEMORY_BIN=/full/path/to/wrapper claude  # launching Claude Code with it set
```

## Slash command

`/code-memory <retrieve|record|reingest|resolve|ingest> [args...]`
forwards to the local CLI. Use it for the cases the hooks don't cover
automatically (force a custom-verdict `record`, ad-hoc query, etc.).

## Bundled skill

`skills/code-memory/SKILL.md` documents the tools, the Context Pack
format, and when the agent should call retrieve / record / reingest /
graph tools manually. Claude Code surfaces it automatically when the
plugin is installed.

## Architecture differences vs the OpenCode plugin

OpenCode plugins are Bun-loaded TypeScript modules that hold in-memory
state across hooks. Claude Code spawns a **fresh shell process per
hook**, so this plugin:

- Uses plain Node.js (no build step, no runtime deps).
- Persists session state on disk under `$XDG_CACHE_HOME/code-memory/claude-plugin/`
  (or `~/.cache/...`).
- Reads/writes that state on every hook entry (`loadSession` /
  `saveSession` / `invalidatePack`).
- Debounces the cross-file resolver via a marker file + a detached
  worker process (`resolver-debounce.js`) instead of a JS `setTimeout`.

Hook ↔ event mapping:

| OpenCode hook                              | Claude Code hook |
| ------------------------------------------ | ---------------- |
| First `chat.message` of a session          | `SessionStart`   |
| `chat.message` + `experimental.chat.system.transform` | `UserPromptSubmit` (does fetch *and* inject in one shot via `additionalContext`) |
| `tool.execute.after` for `write`/`edit`/`patch` | `PostToolUse` matched on `Write|Edit|MultiEdit` |
| `event` `session.idle`                     | `Stop`           |

## Behavior contract

- A missing or broken backend never crashes the session — every hook
  exits cleanly with no output if the CLI is missing.
- Trivial replies ("yes", "ok", "continue") do not trigger retrieval.
- A re-ingest after `Write`/`Edit`/`MultiEdit` is fire-and-forget; the
  agent's turn returns immediately.
- Session episodes are written on `Stop`, with the captured first user
  message and `git diff` as the patch.
- The resolver runs at most once per ~1.5 s burst of writes regardless
  of how many files were touched.

## Technical details — keeping the index fresh

The product proposition collapses if the agent answers from a stale
graph. Code-memory has two completely different states to keep current:

- **Per-file state** — symbol definitions, imports, and call
  expressions for a single source file. Cheap to rebuild from one file.
- **Cross-file state** — the resolved CALLS edges that point a caller
  at the *real* defined Symbol node (instead of a placeholder).
  Touching one file can invalidate edges in many others, so a
  single-file re-ingest is not enough on its own.

Both must move forward together, and edits can come from places the
plugin can't see (vim, IDE saves, `git pull`, `git stash pop`). Below
is the full matrix of failure modes and what the plugin does about
each.

| Failure mode                                              | Mitigation                                                                                                                                                                                       |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Agent rewrites a file → that file's symbols are stale.    | `PostToolUse` fires `code-memory reingest <path>` — tree-sitter re-parses, the file's nodes + edges are dropped and re-inserted, and its Qdrant chunks are replaced.                              |
| Agent rewrites a file → callers in *other* files now point at deleted/renamed symbols. | After every write, the plugin schedules a debounced `code-memory resolve` via `resolver-debounce.js`. The resolver scans the whole graph and re-points placeholder `name::X` CALLS edges to the real Symbol nodes. |
| Agent does a 20-file refactor in 2 seconds → resolver would run 20 times back-to-back. | Resolver scheduling is debounced by a marker file + `RESOLVER_DEBOUNCE_MS` (1.5 s). A new write resets the timer; only the worker that wakes up to a stable marker actually fires `resolve`. |
| File changes outside Claude Code between sessions (vim, IDE, `git pull`, `git checkout`). | `SessionStart` fires a one-shot background `code-memory ingest <cwd>`. The ingest is git-aware and only re-walks files whose hash moved — and it re-runs the resolver. |
| Session-cached Context Pack still reflects pre-write state. | Any successful write also drops the per-session pack cache (`invalidatePack`). The next prompt re-fetches against the just-updated index instead of replaying stale code hits. |
| Backend (FalkorDB / Qdrant / Ollama) is down.             | All CLI calls are guarded by per-command timeouts. Failure is silently no-op'd; the agent's turn is never blocked.                                                                                |
| `code-memory` CLI is missing on PATH.                     | `createMemoryClient` detects this once per hook invocation and short-circuits every method. The plugin stays loaded but inert.                                                                    |
| Agent never explicitly records what it did.               | `Stop` fires `code-memory record` with the first user message + cumulative `git diff` as the patch (verdict = `"idle"`). Future sessions can recall the episode even without manual record.       |

### What is **not** yet covered

- **File deletions via the `Write` tool.** `reingest` on a missing
  path skips cleanly, but the previous File node + DEFINES edges +
  chunks linger until the next git-aware delta ingest evicts them.
- **Renames** look like delete + create to the plugin. The graph keeps
  the old node until the next delta ingest.
- **Bare external module imports** (`@scope/pkg`, `rxjs`). The
  resolver does not chase npm dependencies, so callers into external
  packages stay unresolvable. This is an architectural choice, not a
  freshness bug.
- **Pure reads.** If the agent only opens files, no hook fires.
  That's correct: nothing changed.

### Mental model

Think of the plugin as three concentric refresh loops, each cheaper
and more frequent than the next:

```
session start         every write              every user prompt
     │                     │                         │
     ▼                     ▼                         ▼
delta-ingest        reingest + debounced          retrieve
+ resolver            resolver + pack            (no I/O if
(catches OOB         invalidation               dedup'd within
 edits)              (cross-file edge            60 s)
                      accuracy)
```

If any layer fails, the layer above eventually catches up. The system
is designed so that stale data is a temporary state, not a steady
state.

## Development

The plugin is plain Node.js (no build step, no `node_modules`). To
sanity-check a hook locally:

```bash
echo '{"prompt":"How does getBearerToken work?","cwd":"'"$PWD"'","session_id":"test"}' \
  | node plugins/claude-code/scripts/on-user-prompt.js
```

State is stored under `$XDG_CACHE_HOME/code-memory/claude-plugin/` (or
`~/.cache/code-memory/claude-plugin/` if XDG is unset). Delete it to
reset between local tests:

```bash
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/code-memory/claude-plugin"
```

## License

MIT — see [LICENSE](../../LICENSE).
