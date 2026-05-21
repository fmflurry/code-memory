#!/usr/bin/env bash
# Install the code-memory Claude Code plugin.
#
# Claude Code discovers plugins from marketplaces (`plugin marketplace add`)
# or from a local path registered with `plugin install`. This installer
# offers two convenient modes:
#
#   (default)        symlink into ~/.claude/plugins/code-memory/ so Claude
#                    Code picks it up the next time you start a session.
#   --project        symlink into <cwd>/.claude/plugins/code-memory/ for
#                    project-local install.
#   --target DIR     symlink into a custom directory.
#
# Idempotent. Prints the MCP block to paste into ~/.claude.json if you
# also want the optional MCP server (recommended).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLUGIN_DIR="$REPO_ROOT/plugins/claude-code"

[[ -f "$PLUGIN_DIR/.claude-plugin/plugin.json" ]] || {
  echo "Missing plugin.json — repo layout broken." >&2
  exit 1
}

MODE="global"
TARGET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) MODE="project"; shift ;;
    --target)  TARGET="$2"; MODE="custom"; shift 2 ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--project | --target DIR]

  (default)        install globally at ~/.claude/plugins/code-memory
  --project        install project-local at \$PWD/.claude/plugins/code-memory
  --target DIR     install into DIR/code-memory
EOF
      exit 0
      ;;
    *)
      echo "Unknown flag: $1" >&2
      exit 2
      ;;
  esac
done

case "$MODE" in
  global)  TARGET="$HOME/.claude/plugins" ;;
  project) TARGET="$PWD/.claude/plugins" ;;
esac

mkdir -p "$TARGET"

LINK="$TARGET/code-memory"

if [[ -L "$LINK" ]]; then
  rm "$LINK"
elif [[ -e "$LINK" ]]; then
  echo "Refusing to overwrite real directory at $LINK" >&2
  echo "Move or delete it first, then re-run." >&2
  exit 1
fi

ln -s "$PLUGIN_DIR" "$LINK"
echo "Installed plugin: $LINK -> $PLUGIN_DIR"

echo
if command -v code-memory >/dev/null 2>&1; then
  echo "✓ \`code-memory\` CLI on PATH: $(command -v code-memory)"
else
  cat <<EOF
⚠  \`code-memory\` is NOT on PATH. The plugin will load but every hook
   will no-op until the CLI is installed. Pick one:

     pipx install git+https://github.com/fmflurry/code-memory
     uv tool install git+https://github.com/fmflurry/code-memory

   Or shim uvx into ~/.local/bin/code-memory yourself.
EOF
fi

cat <<'EOF'

────────────────────────────────────────────────────────────────────────
Plugin installed.

If you also want the MCP tools available (recommended — the plugin
covers automatic paths, the MCP server exposes manual calls), add this
to ~/.claude.json under "mcpServers" (project scope works too):

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

Restart Claude Code after editing.
────────────────────────────────────────────────────────────────────────
EOF
