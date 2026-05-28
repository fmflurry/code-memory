#!/usr/bin/env bash
#
# code-memory zero-clone installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/fmflurry/code-memory/main/install.sh | bash
#
# With flags (non-interactive mode):
#   curl -fsSL .../install.sh | bash -s -- \
#     --yes --no-docker --no-ollama --no-claude --no-opencode --no-mcp --no-claims
#
# What it does (idempotent):
#   1. installs `uv` if missing (provides `uvx` + `uv tool`)
#   2. installs the `code-memory` CLI via `uv tool install --from git+<repo>`
#   3. drops docker-compose.yml + .env into $HOME/.code-memory/
#   4. waits for Docker, starts FalkorDB + Qdrant
#   5. waits for Ollama, pulls bge-m3 (+ optional gemma2:9b for claims)
#   6. (optional, default Y) registers Claude Code plugin + MCP
#   7. (optional, default N) installs OpenCode plugin
#
# Contributors hacking on the repo should `git clone` and run
# `scripts/install.sh` (editable install + --symlink).

set -euo pipefail

REPO_URL="${CODEMEMORY_REPO_URL:-https://github.com/fmflurry/code-memory}"
RAW_URL="${CODEMEMORY_RAW_URL:-https://raw.githubusercontent.com/fmflurry/code-memory/main}"
HOME_DIR="${CODEMEMORY_HOME:-$HOME/.code-memory}"
NPM_PKG="${CODEMEMORY_OPENCODE_PKG:-code-memory-opencode}"

# Flag overrides. Empty = "ask interactively". Anything else = explicit answer.
WANT_DOCKER=""
WANT_OLLAMA=""
WANT_CLAUDE=""
WANT_OPENCODE=""
WANT_MCP=""
WANT_CLAIMS=""    # pull gemma2:9b for claim extraction
ASSUME_YES=0
NON_INTERACTIVE=0

for arg in "$@"; do
  case "$arg" in
    --yes|-y)          ASSUME_YES=1 ;;
    --non-interactive) NON_INTERACTIVE=1 ;;
    --docker)          WANT_DOCKER=1 ;;
    --no-docker)       WANT_DOCKER=0 ;;
    --ollama)          WANT_OLLAMA=1 ;;
    --no-ollama)       WANT_OLLAMA=0 ;;
    --claude)          WANT_CLAUDE=1 ;;
    --no-claude)       WANT_CLAUDE=0 ;;
    --opencode)        WANT_OPENCODE=1 ;;
    --no-opencode)     WANT_OPENCODE=0 ;;
    --mcp)             WANT_MCP=1 ;;
    --no-mcp)          WANT_MCP=0 ;;
    --claims)          WANT_CLAIMS=1 ;;
    --no-claims)       WANT_CLAIMS=0 ;;
    -h|--help)         sed -n '1,30p' "$0"; exit 0 ;;
    *) printf '[err] unknown flag: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YEL=$'\033[0;33m'; BLU=$'\033[0;34m'; DIM=$'\033[2m'; RST=$'\033[0m'
step() { printf '\n%s==>%s %s\n' "$BLU" "$RST" "$*"; }
ok()   { printf '%s[ok]%s %s\n'  "$GRN" "$RST" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$YEL" "$RST" "$*"; }
err()  { printf '%s[err]%s %s\n'  "$RED" "$RST" "$*" >&2; }
dim()  { printf '%s  %s%s\n'      "$DIM" "$*" "$RST"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------- interactive helpers ----------
# tty_in: file used to read user answers. /dev/tty when available even under
# `curl | bash`. Falls back to stdin if /dev/tty is absent.
TTY_IN=""
if [ -r /dev/tty ] && [ -w /dev/tty ]; then
  TTY_IN=/dev/tty
elif [ -t 0 ]; then
  TTY_IN=/dev/stdin
fi

interactive() {
  [ "$NON_INTERACTIVE" -eq 0 ] && [ -n "$TTY_IN" ]
}

# ask_yn <prompt> <default Y|N>  → 0 if yes, 1 if no
ask_yn() {
  local prompt="$1" def="$2" ans hint
  case "$def" in Y|y) hint="[Y/n]" ;; *) hint="[y/N]" ;; esac
  if [ "$ASSUME_YES" -eq 1 ]; then
    [ "$def" = "Y" ] || [ "$def" = "y" ] && return 0 || return 1
  fi
  if ! interactive; then
    [ "$def" = "Y" ] || [ "$def" = "y" ] && return 0 || return 1
  fi
  printf '%s%s%s %s ' "$YEL" "?" "$RST" "$prompt $hint"
  IFS= read -r ans <"$TTY_IN" || ans=""
  ans="${ans:-$def}"
  case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

pause_until_present() {
  local cmd="$1" label="$2" url="$3"
  while ! have "$cmd"; do
    warn "$label not found."
    dim "Install from: $url"
    interactive || { warn "non-interactive: skipping $label"; return 1; }
    printf '%s?%s Press Enter once installed (or type %sskip%s to skip): ' "$YEL" "$RST" "$DIM" "$RST"
    local ans=""
    IFS= read -r ans <"$TTY_IN" || ans=""
    [ "$ans" = "skip" ] && return 1
    hash -r 2>/dev/null || true
  done
  return 0
}

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
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
have uv || { err "uv installed but not on PATH; re-open shell and re-run"; exit 3; }
ok "uv ready"

# ---------- 2. code-memory CLI ----------
step "Installing code-memory CLI"
uv tool install --force --from "git+$REPO_URL" flurryx-code-memory
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
if [ -z "$WANT_DOCKER" ]; then
  if ask_yn "Start FalkorDB + Qdrant via Docker?" "Y"; then WANT_DOCKER=1; else WANT_DOCKER=0; fi
fi
if [ "$WANT_DOCKER" -eq 1 ]; then
  step "Starting FalkorDB + Qdrant"
  if pause_until_present docker "Docker" "https://www.docker.com/products/docker-desktop"; then
    # ensure daemon up
    if ! docker info >/dev/null 2>&1; then
      warn "Docker CLI present but daemon not running. Start Docker Desktop."
      if interactive; then
        printf '%s?%s Press Enter once the daemon is up (or %sskip%s): ' "$YEL" "$RST" "$DIM" "$RST"
        local_ans=""
        IFS= read -r local_ans <"$TTY_IN" || local_ans=""
        [ "$local_ans" = "skip" ] && WANT_DOCKER=0
      fi
    fi
    if [ "$WANT_DOCKER" -eq 1 ]; then
      docker compose -f "$HOME_DIR/docker/docker-compose.yml" --project-directory "$HOME_DIR" up -d
      ok "containers up"
      dim "FalkorDB browser: http://localhost:3000"
      dim "Qdrant dashboard: http://localhost:6333/dashboard"
    fi
  else
    warn "docker step skipped"
  fi
else
  warn "docker step skipped"
fi

# ---------- 5. ollama ----------
if [ -z "$WANT_OLLAMA" ]; then
  if ask_yn "Pull embedding model via Ollama?" "Y"; then WANT_OLLAMA=1; else WANT_OLLAMA=0; fi
fi
if [ "$WANT_OLLAMA" -eq 1 ]; then
  step "Embedding model (bge-m3)"
  if pause_until_present ollama "Ollama" "https://ollama.com/download"; then
    # start daemon if not responding
    if ! ollama list >/dev/null 2>&1; then
      (nohup ollama serve >/dev/null 2>&1 &) || true
      for _ in $(seq 1 30); do
        sleep 1
        ollama list >/dev/null 2>&1 && break
      done
    fi
    if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx 'bge-m3'; then
      ok "bge-m3 already present"
    else
      ollama pull bge-m3 && ok "bge-m3 pulled"
    fi

    # optional gemma2:9b for claim extraction
    if [ -z "$WANT_CLAIMS" ]; then
      if ask_yn "Also pull gemma2:9b for user-claim extraction (~5.4 GB)?" "N"; then WANT_CLAIMS=1; else WANT_CLAIMS=0; fi
    fi
    if [ "$WANT_CLAIMS" -eq 1 ]; then
      if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx 'gemma2:9b'; then
        ok "gemma2:9b already present"
      else
        ollama pull gemma2:9b && ok "gemma2:9b pulled"
      fi
    fi
  else
    warn "ollama step skipped"
  fi
else
  warn "ollama step skipped"
fi

# ---------- 6. Claude Code ----------
if [ -z "$WANT_CLAUDE" ]; then
  if ask_yn "Install Claude Code plugin + MCP?" "Y"; then WANT_CLAUDE=1; else WANT_CLAUDE=0; fi
fi
if [ "$WANT_CLAUDE" -eq 1 ]; then
  if have claude; then
    step "Registering Claude Code plugin + MCP"
    claude plugin marketplace add "$REPO_URL" || warn "marketplace add failed (may already be registered)"
    if claude plugin list 2>/dev/null | grep -qE '^[[:space:]]*❯[[:space:]]+code-memory@code-memory'; then
      ok "plugin already installed"
    else
      claude plugin install code-memory@code-memory --scope user
      ok "plugin installed"
    fi

    if [ -z "$WANT_MCP" ]; then WANT_MCP=1; fi
    if [ "$WANT_MCP" -eq 1 ]; then
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
  else
    warn "claude CLI not found — skipping Claude Code plugin"
    dim "Install: https://docs.anthropic.com/claude/docs/claude-code"
  fi
else
  warn "Claude Code step skipped"
fi

# ---------- 7. OpenCode ----------
if [ -z "$WANT_OPENCODE" ]; then
  if ask_yn "Install OpenCode plugin (npm global)?" "N"; then WANT_OPENCODE=1; else WANT_OPENCODE=0; fi
fi
if [ "$WANT_OPENCODE" -eq 1 ]; then
  step "Installing OpenCode plugin"
  if ! have npm; then
    warn "npm not found — skipping. Install Node.js, then: npm i -g $NPM_PKG && code-memory-opencode-install"
  else
    npm i -g "$NPM_PKG"
    if have code-memory-opencode-install; then
      code-memory-opencode-install
    else
      warn "$NPM_PKG installed but code-memory-opencode-install not on PATH"
      warn "Add npm global bin to PATH (npm bin -g) and re-run: code-memory-opencode-install"
    fi
  fi
else
  warn "OpenCode step skipped"
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
