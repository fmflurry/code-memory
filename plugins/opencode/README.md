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
| `chat.message`                        | Detects substantive code intent → `code-memory retrieve --json` → caches a Context Pack per session (5 min TTL, 60 s dedup). |
| `experimental.chat.system.transform`  | Appends a fresh Context Pack to the system prompt.             |
| `tool.execute.after` (`write`/`edit`/`patch`) | Fires `code-memory reingest <path>` in the background. |
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

| Constant          | Default     | Purpose                                                  |
| ----------------- | ----------- | -------------------------------------------------------- |
| `PACK_TTL_MS`     | 5 min       | How long a cached Context Pack stays injectable.         |
| `DEDUP_WINDOW_MS` | 60 s        | Same-query suppression to avoid hammering the embedder.  |
| `WRITE_TOOLS`     | write/edit/patch | Which tool names trigger auto-reingest.            |

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

## License

MIT — see [LICENSE](../../LICENSE).
