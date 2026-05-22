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
SKIP_MCP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) MODE="project"; shift ;;
    --target)  TARGET="$2"; MODE="custom"; shift 2 ;;
    --no-mcp)  SKIP_MCP=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--project | --target DIR] [--no-mcp]

  (default)        install globally at ~/.claude/plugins/code-memory
  --project        install project-local at \$PWD/.claude/plugins/code-memory
  --target DIR     install into DIR/code-memory
  --no-mcp         skip registering the code-memory MCP server with Claude Code
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
  # last resort: `pip install --user uv` if a usable pip is around
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

# ---------- register MCP server with Claude Code ----------
register_mcp() {
  local scope="$1"
  if ! command -v claude >/dev/null 2>&1; then
    cat <<EOF
⚠  \`claude\` CLI not found; cannot auto-register the MCP server.
   Install Claude Code from https://claude.com/claude-code, then run:

     claude mcp add code-memory --scope $scope -e CODE_MEMORY_PROJECT=auto \\
       -- uvx --from git+https://github.com/fmflurry/code-memory code-memory-mcp
EOF
    return 0
  fi
  if claude mcp list 2>/dev/null | grep -qE '^[[:space:]]*code-memory[[:space:]]'; then
    echo "✓ MCP server \`code-memory\` already registered with Claude Code"
    return 0
  fi
  echo "Registering code-memory MCP (scope=$scope)..."
  if claude mcp add code-memory \
      --scope "$scope" \
      -e CODE_MEMORY_PROJECT=auto \
      -- uvx --from git+https://github.com/fmflurry/code-memory code-memory-mcp; then
    echo "✓ MCP registered. Restart Claude Code (or reload the VS Code window) to pick it up."
    if [[ "$scope" == "project" ]]; then
      echo "  (a .mcp.json was written to the project root — commit it so teammates get it too)"
    fi
  else
    echo "✗ \`claude mcp add\` failed; you can add the MCP block manually (see README §MCP server)." >&2
  fi
}

if [[ "$SKIP_MCP" -eq 1 ]]; then
  echo
  echo "Skipping MCP registration (--no-mcp). To enable later, see README §MCP server."
else
  echo
  case "$MODE" in
    project) MCP_SCOPE="project" ;;
    *)       MCP_SCOPE="user" ;;
  esac
  if ensure_uvx; then
    register_mcp "$MCP_SCOPE"
  else
    echo "Skipping MCP registration because uvx is missing." >&2
  fi
fi
