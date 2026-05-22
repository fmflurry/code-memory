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
SKIP_MCP=0

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
    --no-mcp)
      SKIP_MCP=1
      shift
      ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--project | --target DIR] [--no-mcp]

  (default)        install globally at ~/.config/opencode/plugins/
  --project        install project-local at \$PWD/.opencode/plugins/
  --target DIR     install into DIR
  --no-mcp         skip registering the code-memory MCP server in opencode.jsonc
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

# ---------- ensure uvx is available ----------
ensure_uvx() {
  if command -v uvx >/dev/null 2>&1; then
    echo "✓ uvx on PATH: $(command -v uvx)"
    return 0
  fi
  echo "⚠  uvx not on PATH; attempting to install \`uv\` (provides uvx)..."
  if command -v pipx >/dev/null 2>&1; then
    pipx install uv >/dev/null 2>&1 && echo "  installed via pipx" && return 0
  fi
  if command -v brew >/dev/null 2>&1; then
    brew install uv >/dev/null 2>&1 && echo "  installed via brew" && return 0
  fi
  if command -v pip3 >/dev/null 2>&1; then
    pip3 install --user uv >/dev/null 2>&1 && echo "  installed via pip3 --user" && return 0
  elif command -v pip >/dev/null 2>&1; then
    pip install --user uv >/dev/null 2>&1 && echo "  installed via pip --user" && return 0
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
      && echo "  installed via astral.sh/uv (open a new shell so PATH picks up ~/.local/bin)" \
      && return 0
  fi
  cat >&2 <<EOF
✗ Could not auto-install uv/uvx. Install one of:
    pipx install uv
    brew install uv
    pip3 install --user uv
    curl -LsSf https://astral.sh/uv/install.sh | sh
  then re-run this installer (or pass --no-mcp to skip the MCP step).
EOF
  return 1
}

# ---------- register MCP server in opencode.jsonc ----------
register_mcp() {
  # opencode mcp add is interactive, so edit the config file directly.
  local config
  case "$MODE" in
    project) config="$PWD/opencode.jsonc" ;;
    *)       config="$HOME/.config/opencode/opencode.jsonc" ;;
  esac

  local helper="$REPO_ROOT/plugins/opencode/scripts/add-mcp.py"
  local py_bin=""
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    py_bin="$REPO_ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    py_bin="python3"
  else
    echo "⚠  no python3 found; skipping MCP config edit. Add the block manually:" >&2
    cat >&2 <<'EOF'
  "code-memory": {
    "type": "local",
    "command": ["uvx", "--from", "git+https://github.com/fmflurry/code-memory", "code-memory-mcp"],
    "enabled": true,
    "environment": { "CODE_MEMORY_PROJECT": "auto" }
  }
EOF
    return 0
  fi

  echo "Registering code-memory MCP in $config..."
  "$py_bin" "$helper" "$config"
  echo "  Restart OpenCode to pick up the new server."
  if [[ "$MODE" == "project" ]]; then
    echo "  (commit $config so teammates get it too)"
  fi
}

if [[ "$SKIP_MCP" -eq 1 ]]; then
  echo
  echo "Skipping MCP registration (--no-mcp). To enable later, see README §MCP server."
else
  echo
  if ensure_uvx; then
    register_mcp
  else
    echo "Skipping MCP registration because uvx is missing." >&2
  fi
fi

echo "────────────────────────────────────────────────────────────────────────"
echo "Plugin installed."
echo "────────────────────────────────────────────────────────────────────────"
