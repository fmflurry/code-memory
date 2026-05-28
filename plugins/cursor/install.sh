#!/usr/bin/env bash
# Install the code-memory Cursor plugin.
#
# Cursor reads hooks from:
#   - user scope:    ~/.cursor/hooks.json
#   - project scope: <cwd>/.cursor/hooks.json
# and MCP servers from the same directory's mcp.json.
#
# This installer:
#   1. Renders hooks/hooks.json.template with the absolute plugin path.
#   2. Merges the result into the target hooks.json (preserving other hooks).
#   3. Optionally merges an MCP server entry into mcp.json.
#   4. Copies rules/code-memory.mdc into <target>/rules/.
#
# All operations are idempotent — re-running the installer updates paths
# and replaces our entries while leaving foreign entries untouched.
#
# Flags:
#   --scope X    user (default) | project
#   --no-mcp     skip MCP server registration
#   --uninstall  remove our hooks + MCP entry + rule file

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLUGIN_DIR="$REPO_ROOT/plugins/cursor"

[[ -f "$PLUGIN_DIR/hooks/hooks.json.template" ]] || {
  echo "✗ Missing hooks template — repo layout broken." >&2
  exit 1
}

SCOPE="user"
SKIP_MCP=0
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope)     SCOPE="$2"; shift 2 ;;
    --no-mcp)    SKIP_MCP=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--scope user|project] [--no-mcp] [--uninstall]

  --scope X    user (default — writes ~/.cursor/) | project (writes ./.cursor/)
  --no-mcp     skip MCP server registration
  --uninstall  remove our entries and the rule file

The installer renders hooks.json.template with the absolute plugin path,
merges it into the target hooks.json (preserving foreign hooks), and
optionally registers the code-memory MCP server in mcp.json.
EOF
      exit 0
      ;;
    *)
      echo "Unknown flag: $1" >&2
      exit 2
      ;;
  esac
done

if ! command -v node >/dev/null 2>&1; then
  echo "✗ \`node\` not on PATH (required for JSON merging)." >&2
  exit 3
fi

case "$SCOPE" in
  user)    TARGET_DIR="$HOME/.cursor" ;;
  project) TARGET_DIR="$(pwd)/.cursor" ;;
  *)
    echo "✗ --scope must be user or project (got: $SCOPE)" >&2
    exit 2
    ;;
esac

mkdir -p "$TARGET_DIR/rules"

HOOKS_FILE="$TARGET_DIR/hooks.json"
MCP_FILE="$TARGET_DIR/mcp.json"
RULE_FILE="$TARGET_DIR/rules/code-memory.mdc"

# Hook names we own. The merge step replaces these; any other hooks the
# user added stay untouched.
OUR_HOOKS='["sessionStart","sessionEnd","beforeSubmitPrompt","preToolUse","beforeMCPExecution","postToolUse","afterFileEdit","preCompact","stop"]'

# --------------------------------------------------------------- uninstall
if [[ "$UNINSTALL" -eq 1 ]]; then
  if [[ -f "$HOOKS_FILE" ]]; then
    node - "$HOOKS_FILE" "$OUR_HOOKS" "$PLUGIN_DIR" <<'NODE'
const fs = require("node:fs");
const [_, __, file, ours, pluginDir] = process.argv;
const data = JSON.parse(fs.readFileSync(file, "utf8"));
const names = JSON.parse(ours);
if (data && data.hooks) {
  for (const name of names) {
    const arr = data.hooks[name];
    if (!Array.isArray(arr)) continue;
    data.hooks[name] = arr.filter((h) => {
      const cmd = String(h.command || "");
      return !cmd.includes(pluginDir);
    });
    if (data.hooks[name].length === 0) delete data.hooks[name];
  }
  if (Object.keys(data.hooks).length === 0) delete data.hooks;
}
fs.writeFileSync(file, JSON.stringify(data, null, 2) + "\n");
NODE
    echo "✓ removed code-memory hooks from $HOOKS_FILE"
  fi
  if [[ -f "$MCP_FILE" ]]; then
    node - "$MCP_FILE" <<'NODE'
const fs = require("node:fs");
const [_, __, file] = process.argv;
const data = JSON.parse(fs.readFileSync(file, "utf8"));
if (data && data.mcpServers) {
  delete data.mcpServers["code-memory"];
  if (Object.keys(data.mcpServers).length === 0) delete data.mcpServers;
}
fs.writeFileSync(file, JSON.stringify(data, null, 2) + "\n");
NODE
    echo "✓ removed code-memory MCP entry from $MCP_FILE"
  fi
  rm -f "$RULE_FILE"
  echo "✓ removed $RULE_FILE"
  echo "Done. Restart Cursor to pick up the change."
  exit 0
fi

# --------------------------------------------------------------- install
echo "→ Target: $TARGET_DIR (scope=$SCOPE)"

# 1) Render hooks template with absolute plugin path
RENDERED="$(node -e '
  const fs = require("node:fs");
  const [tpl, pdir] = process.argv.slice(1);
  const s = fs.readFileSync(tpl, "utf8").replace(/{{PLUGIN_DIR}}/g, pdir);
  process.stdout.write(s);
' "$PLUGIN_DIR/hooks/hooks.json.template" "$PLUGIN_DIR")"

# 2) Merge into existing hooks.json (or create)
node - "$HOOKS_FILE" "$RENDERED" <<'NODE'
const fs = require("node:fs");
const [_, __, file, renderedJson] = process.argv;
const incoming = JSON.parse(renderedJson);

let existing = { version: 1, hooks: {} };
try {
  existing = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!existing || typeof existing !== "object") existing = { version: 1, hooks: {} };
  if (!existing.hooks || typeof existing.hooks !== "object") existing.hooks = {};
} catch {
  // file missing or invalid — start fresh
}

existing.version = existing.version || incoming.version || 1;

for (const [name, arr] of Object.entries(incoming.hooks || {})) {
  // Drop any prior entries we previously installed (match by command path)
  const prior = Array.isArray(existing.hooks[name]) ? existing.hooks[name] : [];
  const ours = arr.map((h) => h.command);
  // Keep foreign entries (not ours), then append our fresh entries
  const kept = prior.filter((h) => !ours.includes(String(h.command || "")));
  existing.hooks[name] = [...kept, ...arr];
}

fs.mkdirSync(require("node:path").dirname(file), { recursive: true });
fs.writeFileSync(file, JSON.stringify(existing, null, 2) + "\n");
NODE
echo "✓ wrote $HOOKS_FILE"

# 3) Copy rule file
cp "$PLUGIN_DIR/rules/code-memory.mdc" "$RULE_FILE"
echo "✓ wrote $RULE_FILE"

# 4) MCP server registration
if [[ "$SKIP_MCP" -eq 0 ]]; then
  if ! command -v uvx >/dev/null 2>&1; then
    cat >&2 <<EOF
⚠  uvx not on PATH. The MCP entry will still be written but Cursor will
   fail to spawn code-memory-mcp until you install uv:
     pipx install uv     |     brew install uv
     pip3 install --user uv     |     curl -LsSf https://astral.sh/uv/install.sh | sh
EOF
  fi
  node - "$MCP_FILE" <<'NODE'
const fs = require("node:fs");
const [_, __, file] = process.argv;
let existing = { mcpServers: {} };
try {
  existing = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!existing || typeof existing !== "object") existing = { mcpServers: {} };
  if (!existing.mcpServers || typeof existing.mcpServers !== "object") existing.mcpServers = {};
} catch {
  // file missing or invalid
}
existing.mcpServers["code-memory"] = {
  command: "uvx",
  args: [
    "--from",
    "git+https://github.com/fmflurry/code-memory",
    "code-memory-mcp",
  ],
  env: { CODE_MEMORY_PROJECT: "auto" },
};
fs.mkdirSync(require("node:path").dirname(file), { recursive: true });
fs.writeFileSync(file, JSON.stringify(existing, null, 2) + "\n");
NODE
  echo "✓ wrote $MCP_FILE (code-memory MCP entry)"
else
  echo "↪ skipped MCP registration (--no-mcp)"
fi

# 5) CLI presence check
echo
if command -v code-memory >/dev/null 2>&1; then
  echo "✓ \`code-memory\` CLI on PATH: $(command -v code-memory)"
else
  cat <<EOF
⚠  \`code-memory\` CLI is NOT on PATH. The plugin will load but every
   hook will no-op until you install it:

     pipx install git+https://github.com/fmflurry/code-memory
     uv tool install git+https://github.com/fmflurry/code-memory

   Or shim uvx into ~/.local/bin/code-memory.
EOF
fi

echo
echo "Done. Restart Cursor so it picks up the new hooks."
