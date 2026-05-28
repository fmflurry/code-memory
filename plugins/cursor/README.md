# @code-memory/cursor-plugin

Cursor plugin that wires the [`code-memory`](../..) backend into Cursor's
hooks system. Same spirit as the
[Claude Code](../claude-code/README.md) and
[OpenCode](../opencode/README.md) plugins, adapted to Cursor's
event names and IO contract.

It steers the agent toward the index before grep / read / shell,
auto-reingests files the agent writes, records the session as an
episode on stop, and nudges on durable user assertions. The plugin
sits next to the `code-memory` MCP server (registered alongside it)
which exposes the `codememory_*` tools.

## What it does

| Cursor hook                     | Behavior                                                                                                                                                                                                                                  |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sessionStart`                  | Background `code-memory ingest <cwd>` (git delta) so the index reflects out-of-band edits (vim, IDE, `git pull`, `git checkout`) since the last session. Also installs the OS autostart watcher.                                          |
| `beforeSubmitPrompt`            | Resets per-turn gate flags. Captures the first user message for the episode record. Detects durable claim intent and stashes a one-shot nudge to disk.                                                                                    |
| `preToolUse` matcher `Shell\|Read\|Grep` | First-tool gate: if no `codememory_*` MCP call has fired this turn AND we haven't nudged yet, emits a one-shot reminder via `agent_message`. Always allows the tool. |
| `beforeMCPExecution` matcher `codememory_.*` | Marks the gate flag as satisfied so subsequent shell / read / grep calls stay silent. |
| `postToolUse`                   | Drains the pending claim-intent nudge (if any) as `additional_context`. One-shot per turn. |
| `afterFileEdit`                 | (a) Fires `code-memory reingest <file_path>`. (b) Schedules a debounced `code-memory resolve` to re-point cross-file CALLS edges. |
| `preCompact`                    | Records an eager episode before Cursor compacts the context window — Cursor-only safety net. |
| `stop`                          | Records the conversation as an episode (verdict `idle`) with the first user message + `git diff` as the patch. |
| `sessionEnd`                    | Cleans up the per-conversation state file under `$XDG_CACHE_HOME/code-memory/cursor-plugin/`. |

All backend calls are best-effort. If `code-memory` is not on PATH,
every hook degrades to a benign no-op — your Cursor session is never
blocked.

## Requirements

1. **Node 18+** on PATH (used by the hook handlers and by `install.sh`).
2. **`code-memory` CLI** on PATH:
   ```bash
   pipx install git+https://github.com/fmflurry/code-memory
   #   or
   uv tool install git+https://github.com/fmflurry/code-memory
   ```
3. **Running infra**: FalkorDB + Qdrant + Ollama with `bge-m3`. See the
   main [README](../../README.md#installation).
4. The repo must have been ingested at least once:
   ```bash
   code-memory ingest /path/to/repo
   ```

## Install

```bash
# user scope (default) — writes ~/.cursor/hooks.json + mcp.json + rules/
./plugins/cursor/install.sh

# project scope — writes ./.cursor/hooks.json + mcp.json + rules/
./plugins/cursor/install.sh --scope project

# skip MCP server registration
./plugins/cursor/install.sh --no-mcp

# remove our hooks + MCP entry + rule file
./plugins/cursor/install.sh --uninstall
```

The installer:
- Renders `hooks/hooks.json.template` with the absolute plugin path.
- Merges the result into the target `hooks.json` — pre-existing hooks
  you added are preserved. Re-running replaces only our entries.
- Writes a `code-memory` server into `mcp.json` (preserves other servers).
- Copies `rules/code-memory.mdc` into `<target>/rules/`.

**Restart Cursor** after installing so the new hooks take effect.

## Configuration

The plugin reads two environment variables:

| Variable               | Default              | Purpose                                                                                          |
| ---------------------- | -------------------- | ------------------------------------------------------------------------------------------------ |
| `CODE_MEMORY_BIN`      | `code-memory`        | Override the binary name / absolute path.                                                        |
| `CODE_MEMORY_PROJECT`  | (none)               | Forwarded as `--project <slug>` to every CLI call. Useful for monorepos with non-standard slugs. |

Other knobs live as inline constants in the scripts:

| Constant                | File                              | Default | Purpose                                                       |
| ----------------------- | --------------------------------- | ------- | ------------------------------------------------------------- |
| `RESOLVER_DEBOUNCE_MS`  | `scripts/resolver-debounce.js`    | 1.5 s   | Quiet period after the last write before the resolver re-runs. |
| Hook timeouts           | `hooks/hooks.json.template`       | 3–12 s  | Maximum wall-clock per hook invocation.                       |

### Custom binary

If `code-memory` isn't on PATH (for example, you only have `uvx`), shim it:

```bash
# put this in ~/.local/bin/code-memory and chmod +x
exec uvx --from git+https://github.com/fmflurry/code-memory code-memory "$@"
```

## State

Per-conversation state lives under
`$XDG_CACHE_HOME/code-memory/cursor-plugin/` (or
`~/.cache/code-memory/cursor-plugin/` if XDG is unset):

```
sessions/<conv-id>.json     # firstUserMessage, gate flags, pending claim nudge
resolvers/<cwd-hash>.marker # debounce file for the resolver worker
```

Delete it to reset between local tests:

```bash
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/code-memory/cursor-plugin"
```

## Behavior contract

- A missing or broken backend never crashes the session — every hook
  exits cleanly with no output if the CLI is missing.
- A re-ingest after `afterFileEdit` is fire-and-forget; the agent's
  turn returns immediately.
- The resolver runs at most once per ~1.5 s burst of writes regardless
  of how many files were touched.
- Session episodes are written on `preCompact` AND `stop`. Either path
  catches conversations Cursor would otherwise let drop.
- The gate nudge fires at most once per turn. A `codememory_*` MCP call
  silences it for the rest of the turn.

## Cursor-specific limitation

Cursor's `beforeSubmitPrompt` hook **cannot inject context** — its
output is limited to `{continue, user_message}`. So the claim-intent
nudge is stashed to disk on `beforeSubmitPrompt` and drained by the
first `postToolUse` of the turn (where `additional_context` is
allowed). If the agent's reply uses no tools, the nudge does not
surface for that turn; it is dropped at `sessionEnd`. In practice
substantive turns almost always trigger at least one tool call.

## Verify locally

Hook scripts are plain Node — you can sanity-check them with synthetic
input:

```bash
echo '{"conversation_id":"test","prompt":"we use FalkorStore"}' \
  | node plugins/cursor/scripts/on-before-submit-prompt.js
echo '{"conversation_id":"test","tool_name":"Read"}' \
  | node plugins/cursor/scripts/on-pre-tool-use.js
echo '{"conversation_id":"test"}' \
  | node plugins/cursor/scripts/on-post-tool-use.js
```

Run the claim-intent tests:

```bash
node --test plugins/cursor/scripts/lib/claim-intent.test.js
```

## License

MIT — see [LICENSE](../../LICENSE).
