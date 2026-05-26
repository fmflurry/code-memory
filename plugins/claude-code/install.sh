#!/usr/bin/env bash
# Install the code-memory Claude Code plugin.
#
# Claude Code only loads plugins that are registered with its plugin loader
# (entries in `~/.claude/plugins/installed_plugins.json`). Symlinking the
# repo into `~/.claude/plugins/code-memory/` is NOT enough — the hooks
# defined in `hooks/hooks.json` will never fire.
#
# So this installer:
#   1. Registers the repo root as a local Claude Code marketplace via
#      `claude plugin marketplace add <repo>`. The marketplace manifest
#      lives at `<repo>/.claude-plugin/marketplace.json` and points at
#      `./plugins/claude-code`.
#   2. Installs the plugin via `claude plugin install code-memory@code-memory`.
#   3. Optionally registers the `code-memory` MCP server with Claude Code.
#
# All steps are idempotent: re-running the installer updates the
# marketplace pointer and re-installs the plugin (which is what you want
# after pulling new commits).
#
# Flags:
#   --no-mcp     skip MCP server registration
#   --scope X    plugin scope, one of: user | project (default: user)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLUGIN_DIR="$REPO_ROOT/plugins/claude-code"
MARKETPLACE_MANIFEST="$REPO_ROOT/.claude-plugin/marketplace.json"

[[ -f "$PLUGIN_DIR/.claude-plugin/plugin.json" ]] || {
  echo "Missing plugin.json — repo layout broken." >&2
  exit 1
}
[[ -f "$MARKETPLACE_MANIFEST" ]] || {
  echo "Missing marketplace manifest at $MARKETPLACE_MANIFEST" >&2
  exit 1
}

SCOPE="user"
SKIP_MCP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-mcp)     SKIP_MCP=1; shift ;;
    --scope)      SCOPE="$2"; shift 2 ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--scope user|project] [--no-mcp]

  --scope X       plugin install scope (default: user)
  --no-mcp        skip registering the code-memory MCP server

This script registers $REPO_ROOT as a local marketplace, installs the
\`code-memory\` plugin from it, and (optionally) wires up the MCP server.
EOF
      exit 0
      ;;
    *)
      echo "Unknown flag: $1" >&2
      exit 2
      ;;
  esac
done

if ! command -v claude >/dev/null 2>&1; then
  cat >&2 <<EOF
✗ \`claude\` CLI not found on PATH.
  Install Claude Code from https://claude.com/claude-code, then re-run.
EOF
  exit 3
fi

# ---------- validate manifests ----------
echo "→ Validating manifests..."
claude plugin validate "$PLUGIN_DIR" >/dev/null
claude plugin validate "$REPO_ROOT" >/dev/null
echo "✓ Manifests valid"

# ---------- register marketplace ----------
# Marketplace name is whatever marketplace.json declares (`code-memory`).
# `claude plugin marketplace add` is idempotent — repeated calls just
# refresh the source pointer.
echo "→ Registering local marketplace ($REPO_ROOT)..."
if claude plugin marketplace list 2>/dev/null \
     | grep -qE '^[[:space:]]*❯[[:space:]]+code-memory[[:space:]]*$'; then
  echo "✓ marketplace 'code-memory' already registered (skipping add)"
else
  claude plugin marketplace add "$REPO_ROOT"
fi

# ---------- install plugin ----------
echo "→ Installing plugin code-memory@code-memory (scope=$SCOPE)..."
if claude plugin list 2>/dev/null \
     | grep -qE '^[[:space:]]*❯[[:space:]]+code-memory@code-memory[[:space:]]*$'; then
  echo "✓ plugin already installed — refreshing via uninstall+install"
  claude plugin uninstall code-memory@code-memory >/dev/null 2>&1 || true
fi
claude plugin install code-memory@code-memory --scope "$SCOPE"

# ---------- CLI presence check (best-effort) ----------
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

# ---------- register MCP server with Claude Code ----------
register_mcp() {
  local scope="$1"
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
  if ensure_uvx; then
    register_mcp "$SCOPE"
  else
    echo "Skipping MCP registration because uvx is missing." >&2
  fi
fi

echo
echo "Done. Restart Claude Code so it picks up the new hooks."
