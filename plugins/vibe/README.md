# @code-memory/vibe-plugin

Mistral Vibe plugin that wires the [`code-memory`](../..) backend into Vibe.
Same spirit as the [Claude Code](../claude-code/README.md),
[Cursor](../cursor/README.md), and [OpenCode](../opencode/README.md) plugins,
adapted to what Vibe actually exposes.

## The honest mapping

Vibe is extensible through **Skills**, **MCP servers**, **custom agents**, and
**custom prompts** — but it has **no lifecycle-hook API**. The Claude Code and
Cursor plugins lean on hooks (`sessionStart`, `postToolUse`, `afterFileEdit`,
`stop`, `preCompact`, …) for per-turn automation: auto-reingest on write,
episode recording on stop, the retrieve-before-grep nudge. None of that can
fire from inside Vibe.

So this plugin delivers the same capability through the surfaces Vibe has:

| code-memory plugin piece                       | Vibe equivalent                                            |
| ----------------------------------------------- | ---------------------------------------------------------- |
| MCP server (`codememory_*` tools)               | `[[mcp_servers]]` block in `config.toml`                   |
| `/code-memory` slash command                    | `skills/code-memory/SKILL.md` (`user-invocable: true`)     |
| Context rule (retrieve-before-grep steering)    | The same skill's body — standing orientation guidance      |
| `afterFileEdit` → auto-reingest hook            | **OS autostart watcher** (`code-memory autostart install`) |
| `sessionStart` / `stop` / `preCompact` episodes | _no equivalent_ — drive `code-memory record` manually      |

The OS watcher is harness-independent, so it covers the file-edit → reingest
loop that Vibe cannot hook. Episode recording on session events has no host
trigger; use `/code-memory` or the CLI to record manually when it matters.

## What it installs

1. **Skill** at `<vibe>/skills/code-memory/SKILL.md` — auto-discovered,
   exposed as `/code-memory`, and read as orientation guidance that steers the
   agent to the index first.
2. **MCP server** — a managed `[[mcp_servers]]` block in `config.toml`
   spawning `code-memory-mcp` via `uvx`.
3. **OS autostart watcher** for the current repo, unless `--no-watch`.

## Requirements

1. **`uvx`** (from [uv](https://docs.astral.sh/uv/)) on PATH, to spawn the MCP
   server. `pipx install uv` / `brew install uv` / `curl -LsSf https://astral.sh/uv/install.sh | sh`.
2. **`code-memory` CLI** on PATH (for `/code-memory` and the watcher):
   ```bash
   pipx install git+https://github.com/fmflurry/code-memory
   #   or
   uv tool install git+https://github.com/fmflurry/code-memory
   ```
3. **Running infra**: FalkorDB + Qdrant + Ollama with `bge-m3`. See the main
   [README](../../README.md#installation).
4. The repo must have been ingested at least once:
   ```bash
   code-memory ingest /path/to/repo
   ```

## Install

```bash
# user scope (default) — writes ${VIBE_HOME:-~/.vibe}/config.toml + skills/
./plugins/vibe/install.sh

# project scope — writes ./.vibe/config.toml + skills/
./plugins/vibe/install.sh --scope project

# skip MCP server registration
./plugins/vibe/install.sh --no-mcp

# skip the OS autostart watcher
./plugins/vibe/install.sh --no-watch

# remove the skill, our MCP block, and the watcher
./plugins/vibe/install.sh --uninstall
```

The installer:
- Copies the skill into `<vibe>/skills/code-memory/`.
- Appends a managed `code-memory` `[[mcp_servers]]` block to `config.toml`
  between sentinel comments — foreign config is preserved, re-running replaces
  only our block.
- Installs the OS autostart watcher for the current repo.

**Restart Vibe** after installing so it re-scans skills + MCP servers.

## The MCP block

```toml
# >>> code-memory (managed by plugins/vibe/install.sh) >>>
[[mcp_servers]]
name = "code-memory"
transport = "stdio"
command = "uvx"
args = ["--from", "git+https://github.com/fmflurry/code-memory", "code-memory-mcp"]
# <<< code-memory <<<
```

The server auto-detects the project slug from its working directory
(`detect_project_slug()`), so no environment variable is required. Each
`codememory_*` tool still takes an explicit `project` argument — the repo-root
basename, never `auto`/`default`.

## Verify locally

```bash
# Skill is discovered + slash command exists
ls "${VIBE_HOME:-$HOME/.vibe}/skills/code-memory/SKILL.md"

# MCP block is present
grep -A6 'code-memory (managed' "${VIBE_HOME:-$HOME/.vibe}/config.toml"

# Watcher is registered for this repo
code-memory autostart status .
```

## License

MIT — see [LICENSE](../../LICENSE).
