#!/usr/bin/env bash
# Install the code-memory Mistral Vibe plugin.
#
# Mistral Vibe has no lifecycle-hook API (unlike Claude Code / Cursor), so this
# plugin wires code-memory in through the three surfaces Vibe *does* expose:
#
#   1. A Skill (skills/code-memory/SKILL.md) — auto-discovered, exposed as the
#      `/code-memory` slash command, and standing orientation guidance that
#      steers the agent toward the index before grep/read/shell.
#   2. The code-memory MCP server, registered as a managed `[[mcp_servers]]`
#      block in config.toml (exposes the `codememory_*` tools).
#   3. The OS-level autostart watcher (`code-memory autostart install`) — the
#      background auto-reingest that replaces the per-turn hooks Vibe lacks.
#
# Vibe reads config + skills from:
#   - user scope:    ${VIBE_HOME:-~/.vibe}/{config.toml,skills/}
#   - project scope: <cwd>/.vibe/{config.toml,skills/}
#
# All operations are idempotent. The MCP entry lives between sentinel comments
# so re-running replaces only our block and leaves foreign config untouched.
#
# Flags:
#   --scope X    user (default) | project
#   --no-mcp     skip MCP server registration
#   --no-watch   skip installing the OS autostart watcher
#   --uninstall  remove the skill, our MCP block, and the watcher

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLUGIN_DIR="$REPO_ROOT/plugins/vibe"
SKILL_SRC="$PLUGIN_DIR/skills/code-memory"

[[ -f "$SKILL_SRC/SKILL.md" ]] || {
  echo "✗ Missing skills/code-memory/SKILL.md — repo layout broken." >&2
  exit 1
}

SCOPE="user"
SKIP_MCP=0
SKIP_WATCH=0
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope)     SCOPE="$2"; shift 2 ;;
    --no-mcp)    SKIP_MCP=1; shift ;;
    --no-watch)  SKIP_WATCH=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--scope user|project] [--no-mcp] [--no-watch] [--uninstall]

  --scope X    user (default — writes \${VIBE_HOME:-~/.vibe}/) | project (./.vibe/)
  --no-mcp     skip MCP server registration in config.toml
  --no-watch   skip installing the OS autostart watcher
  --uninstall  remove the skill, our config.toml MCP block, and the watcher

The installer copies the code-memory skill, merges a managed code-memory MCP
block into config.toml (preserving foreign config), and installs the OS
autostart watcher so file edits are re-ingested in the background — Vibe has
no hooks to do that per-turn.
EOF
      exit 0
      ;;
    *)
      echo "Unknown flag: $1" >&2
      exit 2
      ;;
  esac
done

case "$SCOPE" in
  user)    TARGET_DIR="${VIBE_HOME:-$HOME/.vibe}" ;;
  project) TARGET_DIR="$(pwd)/.vibe" ;;
  *)
    echo "✗ --scope must be user or project (got: $SCOPE)" >&2
    exit 2
    ;;
esac

CONFIG_FILE="$TARGET_DIR/config.toml"
SKILL_DST="$TARGET_DIR/skills/code-memory"
BEGIN_MARK="# >>> code-memory (managed by plugins/vibe/install.sh) >>>"
END_MARK="# <<< code-memory <<<"

# Strip our managed block from config.toml (in place, leaving a trailing
# newline). No-op if the file or block is absent.
strip_mcp_block() {
  [[ -f "$CONFIG_FILE" ]] || return 0
  local tmp
  tmp="$(mktemp)"
  awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
    $0 == b { skip = 1; next }
    $0 == e { skip = 0; next }
    !skip   { print }
  ' "$CONFIG_FILE" > "$tmp"
  mv "$tmp" "$CONFIG_FILE"
}

# --------------------------------------------------------------- uninstall
if [[ "$UNINSTALL" -eq 1 ]]; then
  strip_mcp_block
  [[ -f "$CONFIG_FILE" ]] && echo "✓ removed code-memory MCP block from $CONFIG_FILE"
  rm -rf "$SKILL_DST"
  echo "✓ removed $SKILL_DST"
  if [[ "$SKIP_WATCH" -eq 0 ]] && command -v code-memory >/dev/null 2>&1; then
    code-memory autostart uninstall "$(pwd)" >/dev/null 2>&1 \
      && echo "✓ removed OS autostart watcher for $(pwd)" \
      || echo "↪ no autostart watcher to remove (or removal failed)"
  fi
  echo "Done. Restart Vibe to pick up the change."
  exit 0
fi

# --------------------------------------------------------------- install
echo "→ Target: $TARGET_DIR (scope=$SCOPE)"
mkdir -p "$TARGET_DIR/skills"

# 1) Install the skill
rm -rf "$SKILL_DST"
mkdir -p "$SKILL_DST"
cp "$SKILL_SRC/SKILL.md" "$SKILL_DST/SKILL.md"
echo "✓ wrote $SKILL_DST/SKILL.md"

# 2) MCP server registration (managed block)
if [[ "$SKIP_MCP" -eq 0 ]]; then
  if ! command -v uvx >/dev/null 2>&1; then
    cat >&2 <<EOF
⚠  uvx not on PATH. The MCP block will still be written but Vibe will fail to
   spawn code-memory-mcp until you install uv:
     pipx install uv     |     brew install uv
     pip3 install --user uv     |     curl -LsSf https://astral.sh/uv/install.sh | sh
EOF
  fi
  mkdir -p "$TARGET_DIR"
  strip_mcp_block
  # Ensure a clean separator before appending.
  if [[ -s "$CONFIG_FILE" ]]; then printf '\n' >> "$CONFIG_FILE"; fi
  cat >> "$CONFIG_FILE" <<EOF
$BEGIN_MARK
[[mcp_servers]]
name = "code-memory"
transport = "stdio"
command = "uvx"
args = ["--from", "git+https://github.com/fmflurry/code-memory", "code-memory-mcp"]
$END_MARK
EOF
  echo "✓ wrote $CONFIG_FILE (code-memory MCP block)"
else
  echo "↪ skipped MCP registration (--no-mcp)"
fi

# 3) OS autostart watcher — the per-turn-hook replacement
if [[ "$SKIP_WATCH" -eq 0 ]]; then
  if command -v code-memory >/dev/null 2>&1; then
    if code-memory autostart install "$(pwd)" >/dev/null 2>&1; then
      echo "✓ installed OS autostart watcher for $(pwd)"
    else
      echo "⚠ could not install autostart watcher (run: code-memory autostart install .)"
    fi
  else
    echo "↪ skipped watcher — code-memory CLI not on PATH"
  fi
else
  echo "↪ skipped autostart watcher (--no-watch)"
fi

# 4) CLI presence check
echo
if command -v code-memory >/dev/null 2>&1; then
  echo "✓ \`code-memory\` CLI on PATH: $(command -v code-memory)"
else
  cat <<EOF
⚠  \`code-memory\` CLI is NOT on PATH. The skill + MCP server still load, but
   the /code-memory command and the watcher need it:

     pipx install git+https://github.com/fmflurry/code-memory
     uv tool install git+https://github.com/fmflurry/code-memory
EOF
fi

echo
echo "Done. Restart Vibe so it picks up the new skill + MCP server."
