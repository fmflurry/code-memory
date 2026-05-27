#!/usr/bin/env bash
#
# code-memory zero-clone installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/fmflurry/code-memory/main/install.sh | bash
#
# Or, with options:
#   curl -fsSL https://raw.githubusercontent.com/fmflurry/code-memory/main/install.sh \
#     | bash -s -- --no-docker --no-ollama --no-claude --no-opencode --no-mcp
#
# What it does (idempotent):
#   1. installs `uv` if missing (provides `uvx` + `uv tool`)
#   2. installs the `code-memory` CLI via `uv tool install --from git+<repo>`
#   3. drops docker-compose.yml + .env into $HOME/.code-memory/
#   4. starts FalkorDB + Qdrant via docker compose
#   5. ensures Ollama is running and pulls bge-m3
#   6. registers the Claude Code marketplace + plugin + MCP server
#   7. installs the OpenCode plugin from npm and runs its installer
#
# Contributors who want to hack on the repo should still `git clone` and run
# `scripts/install.sh` (editable install + plugin symlinks via --symlink).

set -euo pipefail

REPO_URL="${CODEMEMORY_REPO_URL:-https://github.com/fmflurry/code-memory}"
RAW_URL="${CODEMEMORY_RAW_URL:-https://raw.githubusercontent.com/fmflurry/code-memory/main}"
HOME_DIR="${CODEMEMORY_HOME:-$HOME/.code-memory}"
NPM_PKG="${CODEMEMORY_OPENCODE_PKG:-code-memory-opencode}"

SKIP_DOCKER=0
SKIP_OLLAMA=0
SKIP_CLAUDE=0
SKIP_OPENCODE=0
SKIP_MCP=0

for arg in "$@"; do
  case "$arg" in
    --no-docker)   SKIP_DOCKER=1 ;;
    --no-ollama)   SKIP_OLLAMA=1 ;;
    --no-claude)   SKIP_CLAUDE=1 ;;
    --no-opencode) SKIP_OPENCODE=1 ;;
    --no-mcp)      SKIP_MCP=1 ;;
    -h|--help)     sed -n '1,28p' "$0"; exit 0 ;;
    *) printf '[err] unknown flag: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YEL=$'\033[0;33m'; BLU=$'\033[0;34m'; DIM=$'\033[2m'; RST=$'\033[0m'
step() { printf '\n%s==>%s %s\n' "$BLU" "$RST" "$*"; }
ok()   { printf '%s[ok]%s %s\n'  "$GRN" "$RST" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$YEL" "$RST" "$*"; }
err()  { printf '%s[err]%s %s\n'  "$RED" "$RST" "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------- 1. uv ----------
step "Ensuring uv is installed"
if have uv; then
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else
  if have curl; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  else
    err "neither uv nor curl present — install curl or uv manually then re-run"
    exit 3
  fi
fi
# uv installer drops binary in ~/.local/bin or ~/.cargo/bin
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
have uv || { err "uv installed but not on PATH; re-open shell and re-run"; exit 3; }
ok "uv ready"

# ---------- 2. code-memory CLI ----------
step "Installing code-memory CLI"
uv tool install --force --from "git+$REPO_URL" code-memory
ok "code-memory CLI: $(command -v code-memory 2>/dev/null || echo '~/.local/bin/code-memory')"

# ---------- 3. side files ----------
step "Writing infra files to $HOME_DIR"
mkdir -p "$HOME_DIR/docker"
curl -fsSL "$RAW_URL/docker/docker-compose.yml" -o "$HOME_DIR/docker/docker-compose.yml"
ok "wrote $HOME_DIR/docker/docker-compose.yml"
if [ ! -f "$HOME_DIR/.env" ]; then
  curl -fsSL "$RAW_URL/.env.example" -o "$HOME_DIR/.env"
  ok "wrote $HOME_DIR/.env (from .env.example)"
else
  ok ".env already present (not overwritten)"
fi

# ---------- 4. docker ----------
if [ "$SKIP_DOCKER" -eq 0 ]; then
  step "Starting FalkorDB + Qdrant"
  if ! have docker; then
    warn "docker not found — install Docker Desktop and re-run with --no-docker omitted"
  else
    docker compose -f "$HOME_DIR/docker/docker-compose.yml" --project-directory "$HOME_DIR" up -d
    ok "containers up"
    printf '%s  FalkorDB browser: http://localhost:3000\n  Qdrant dashboard: http://localhost:6333/dashboard%s\n' "$DIM" "$RST"
  fi
else
  warn "docker step skipped"
fi

# ---------- 5. ollama ----------
if [ "$SKIP_OLLAMA" -eq 0 ]; then
  step "Embedding model (bge-m3)"
  if ! have ollama; then
    warn "ollama not found. Install from https://ollama.com/download, then: ollama pull bge-m3"
  else
    if ollama list 2>/dev/null | awk '{print $1}' | grep -q '^bge-m3'; then
      ok "bge-m3 already present"
    else
      ollama pull bge-m3 && ok "bge-m3 pulled"
    fi
  fi
else
  warn "ollama step skipped"
fi

# ---------- 6. Claude Code ----------
if [ "$SKIP_CLAUDE" -eq 0 ] && have claude; then
  step "Registering Claude Code plugin + MCP"
  claude plugin marketplace add "$REPO_URL" || warn "marketplace add failed (may already be registered)"
  if claude plugin list 2>/dev/null | grep -qE '^[[:space:]]*❯[[:space:]]+code-memory@code-memory'; then
    ok "plugin already installed"
  else
    claude plugin install code-memory@code-memory --scope user
    ok "plugin installed"
  fi

  if [ "$SKIP_MCP" -eq 0 ]; then
    if claude mcp list 2>/dev/null | grep -qE '^[[:space:]]*code-memory[[:space:]]'; then
      ok "MCP already registered"
    else
      claude mcp add code-memory \
        --scope user \
        -e CODE_MEMORY_PROJECT=auto \
        -- uvx --from "git+$REPO_URL" code-memory-mcp \
        && ok "MCP registered (restart Claude Code to pick it up)" \
        || warn "claude mcp add failed; see README §MCP server"
    fi
  fi
elif [ "$SKIP_CLAUDE" -eq 0 ]; then
  warn "claude CLI not found — skipping Claude Code plugin"
fi

# ---------- 7. OpenCode ----------
if [ "$SKIP_OPENCODE" -eq 0 ]; then
  step "Installing OpenCode plugin"
  if ! have npm; then
    warn "npm not found — skipping. Install node, then: npm i -g $NPM_PKG && code-memory-opencode-install"
  else
    npm i -g "$NPM_PKG"
    if have code-memory-opencode-install; then
      code-memory-opencode-install
    else
      warn "$NPM_PKG installed but code-memory-opencode-install not on PATH"
      warn "Add npm global bin to PATH (npm bin -g) and re-run: code-memory-opencode-install"
    fi
  fi
fi

# ---------- done ----------
step "Done"
cat <<EOF

  Side files:    $HOME_DIR/
  CLI:           $(command -v code-memory 2>/dev/null || echo 'code-memory (not on PATH)')

  Ingest a repo:
    code-memory ingest /path/to/repo

  Query:
    code-memory retrieve "where is the auth middleware?"

  Browse:
    FalkorDB  http://localhost:3000
    Qdrant    http://localhost:6333/dashboard

  Edit defaults: $HOME_DIR/.env
EOF
