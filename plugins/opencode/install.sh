#!/usr/bin/env bash
# Install the code-memory OpenCode plugin into the user's plugin directory.
#
# Default target: ~/.config/opencode/plugins/   (global)
# Override:       --project   -> ./.opencode/plugins/   (cwd, project-local)
#                 --target DIR -> custom directory
#
# Idempotent: re-running re-creates symlinks. Does not edit opencode.jsonc;
# instead it prints the MCP block you need to paste in yourself.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLUGIN_SRC="$REPO_ROOT/plugins/opencode/src"
ENTRY="$PLUGIN_SRC/code-memory.ts"
LIB="$PLUGIN_SRC/code-memory-lib"

TARGET=""
MODE="global"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      MODE="project"
      shift
      ;;
    --target)
      TARGET="$2"
      MODE="custom"
      shift 2
      ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--project | --target DIR]

  (default)        install globally at ~/.config/opencode/plugins/
  --project        install project-local at \$PWD/.opencode/plugins/
  --target DIR     install into DIR
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
  global)  TARGET="$HOME/.config/opencode/plugins" ;;
  project) TARGET="$PWD/.opencode/plugins" ;;
esac

[[ -f "$ENTRY" ]] || { echo "Missing $ENTRY — repo layout broken." >&2; exit 1; }
[[ -d "$LIB"   ]] || { echo "Missing $LIB — repo layout broken." >&2; exit 1; }

mkdir -p "$TARGET"

link_or_replace() {
  local src="$1" dst="$2"
  if [[ -L "$dst" ]]; then
    rm "$dst"
  elif [[ -e "$dst" ]]; then
    echo "Refusing to overwrite real file at $dst" >&2
    echo "Move or delete it first, then re-run." >&2
    exit 1
  fi
  ln -s "$src" "$dst"
  echo "  $dst -> $src"
}

echo "Installing plugin into: $TARGET"
link_or_replace "$ENTRY" "$TARGET/code-memory.ts"
link_or_replace "$LIB"   "$TARGET/code-memory-lib"

# Quick CLI sanity check
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
Plugin installed. To also enable the MCP tools (recommended), add this
block under "mcp": {} in your opencode.jsonc:

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

Restart OpenCode after editing.
────────────────────────────────────────────────────────────────────────
EOF
