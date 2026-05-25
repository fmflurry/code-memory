#!/usr/bin/env bash
#
# code-memory installer (macOS / Linux)
#
# Usage:
#   ./scripts/install.sh                 # full install (interactive prompts)
#   ./scripts/install.sh --no-docker     # skip docker compose
#   ./scripts/install.sh --no-ollama     # skip ollama pull
#   ./scripts/install.sh --no-tests      # skip smoke tests
#   ./scripts/install.sh --with-claims   # also pull gemma2:9b for Graphiti-style
#                                        # user-claim extraction (~5.4 GB)
#   ./scripts/install.sh --with-rerank   # install [rerank] extra (sentence-transformers + torch, ~1.5 GB)
#   ./scripts/install.sh --with-hybrid   # install [hybrid] extra (FlagEmbedding + torch, m3 dense+sparse)
#   ./scripts/install.sh --with-dotnet   # install [dotnet] extra (dnfile, ~200 KB; .NET DLL metadata indexing)
#   ./scripts/install.sh --extras=none   # bypass interactive prompt; install only [dev]
#   ./scripts/install.sh --plugins=opencode,claudecode
#                                        # install named harness plugins (non-interactive)
#   ./scripts/install.sh --plugins=all   # install both
#   ./scripts/install.sh --plugins=none  # skip plugin step entirely
#   ./scripts/install.sh --plugins-scope=project
#                                        # install plugins project-local (./.opencode/ or ./.claude/)
#                                        # default scope is global (~/.config/opencode or ~/.claude)
#
set -euo pipefail

# ---------- helpers ----------
RED=$'\033[0;31m'
GRN=$'\033[0;32m'
YEL=$'\033[0;33m'
BLU=$'\033[0;34m'
DIM=$'\033[2m'
RST=$'\033[0m'

step()    { printf "\n${BLU}==>${RST} %s\n" "$*"; }
ok()      { printf "${GRN}[ok]${RST} %s\n" "$*"; }
warn()    { printf "${YEL}[warn]${RST} %s\n" "$*"; }
err()     { printf "${RED}[err]${RST} %s\n" "$*" >&2; }
die()     { err "$*"; exit 1; }
have()    { command -v "$1" >/dev/null 2>&1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ---------- flags ----------
SKIP_DOCKER=0
SKIP_OLLAMA=0
SKIP_TESTS=0
SKIP_MCP=0
WITH_RERANK=0
WITH_HYBRID=0
WITH_DOTNET=0
WITH_CLAIMS=0
EXTRAS_ARG=""        # empty = interactive; "none" = skip extras prompt; other ignored
PLUGINS_ARG=""       # empty = interactive; explicit value bypasses prompt
PLUGINS_SCOPE="global"  # global | project
for arg in "$@"; do
  case "$arg" in
    --no-docker) SKIP_DOCKER=1 ;;
    --no-ollama) SKIP_OLLAMA=1 ;;
    --no-tests)  SKIP_TESTS=1 ;;
    --no-mcp)    SKIP_MCP=1 ;;
    --with-rerank) WITH_RERANK=1 ;;
    --with-hybrid) WITH_HYBRID=1 ;;
    --with-dotnet) WITH_DOTNET=1 ;;
    --with-claims) WITH_CLAIMS=1 ;;
    --extras=*)         EXTRAS_ARG="${arg#--extras=}" ;;
    --plugins=*)        PLUGINS_ARG="${arg#--plugins=}" ;;
    --plugins-scope=*)  PLUGINS_SCOPE="${arg#--plugins-scope=}" ;;
    -h|--help)
      sed -n '1,22p' "$0"; exit 0 ;;
    *) die "unknown flag: $arg" ;;
  esac
done

case "$PLUGINS_SCOPE" in
  global|project) ;;
  *) die "invalid --plugins-scope=$PLUGINS_SCOPE (expected global|project)" ;;
esac

# ---------- 1. prereqs ----------
step "Checking prerequisites"

PYTHON_BIN=""
for candidate in python3.12 python3.11 python3; do
  if have "$candidate"; then
    ver="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    major="${ver%%.*}"; minor="${ver##*.}"
    if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
      PYTHON_BIN="$candidate"
      ok "Python $ver ($candidate)"
      break
    fi
  fi
done
[ -n "$PYTHON_BIN" ] || die "Python 3.11+ not found. Install from https://www.python.org/."

if [ "$SKIP_DOCKER" -eq 0 ]; then
  have docker || die "Docker not found. Install Docker Desktop: https://www.docker.com/"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 not found (need 'docker compose')."
  ok "Docker $(docker --version | awk '{print $3}' | tr -d ,)"
fi

if [ "$SKIP_OLLAMA" -eq 0 ]; then
  if ! have ollama; then
    warn "Ollama not found — attempting auto-install..."
    OS_KERNEL="$(uname -s)"
    case "$OS_KERNEL" in
      Darwin)
        if have brew; then
          # cask installs the menu-bar app + CLI shim and auto-starts the daemon
          brew install --cask ollama \
            && ok "Ollama installed via brew (cask)" \
            || warn "brew install --cask ollama failed"
        else
          warn "Homebrew not found on macOS. Install brew from https://brew.sh, or download Ollama directly:"
          warn "  https://ollama.com/download/mac"
        fi
        ;;
      Linux)
        if have curl; then
          curl -fsSL https://ollama.com/install.sh | sh \
            && ok "Ollama installed via official script (systemd unit set up)" \
            || warn "Ollama install script failed"
        else
          warn "curl not found. Install curl, then re-run, or download from https://ollama.com/download/linux"
        fi
        ;;
      *)
        warn "Unsupported OS ($OS_KERNEL); install Ollama manually from https://ollama.com/download"
        ;;
    esac

    if ! have ollama; then
      warn "Ollama still not on PATH after install attempt — skipping model pull."
      SKIP_OLLAMA=1
    else
      ok "Ollama $(ollama --version 2>/dev/null | head -1 | awk '{print $NF}')"
    fi
  else
    ok "Ollama $(ollama --version 2>/dev/null | head -1 | awk '{print $NF}')"
  fi
fi

# uvx (from astral `uv`) — needed by the MCP server registration step.
# Plugin installers will retry the install themselves; we surface it here
# so the user can fix PATH issues *before* running the long parts.
if [ "$SKIP_MCP" -eq 0 ]; then
  if have uvx; then
    ok "uvx $(uvx --version 2>/dev/null | head -1)"
  else
    warn "uvx not found on PATH (provides the MCP server entrypoint)."
    INSTALLER_FOUND=0
    have pipx  && { printf "${DIM}  found pipx  — can run: pipx install uv${RST}\n"; INSTALLER_FOUND=1; }
    have brew  && { printf "${DIM}  found brew  — can run: brew install uv${RST}\n"; INSTALLER_FOUND=1; }
    have curl  && { printf "${DIM}  found curl  — can run: curl -LsSf https://astral.sh/uv/install.sh | sh${RST}\n"; INSTALLER_FOUND=1; }
    if [ "$INSTALLER_FOUND" -eq 0 ]; then
      warn "no installer for uv available (pipx / brew / curl all missing)."
      warn "MCP registration will be skipped. Install one of:"
      warn "  brew install pipx     (then: pipx install uv)"
      warn "  brew install uv"
      warn "  install curl, then:   curl -LsSf https://astral.sh/uv/install.sh | sh"
      SKIP_MCP=1
    else
      printf "${DIM}  plugin installer will attempt to install uv automatically.${RST}\n"
    fi
  fi
fi

# ---------- 2. python venv ----------
step "Creating Python virtual environment"
if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
  ok "Created .venv"
else
  ok ".venv already exists"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel >/dev/null
ok "pip upgraded"

# ---------- 3. package install ----------
# Decide optional extras before the install call so we pay the wheel
# download cost exactly once.
prompt_yes_no() {
  # $1 = prompt text, $2 = default (y|n)
  local prompt="$1" default="$2" ans
  local hint="[y/N]"
  [ "$default" = "y" ] && hint="[Y/n]"
  read -r -p "  $prompt $hint " ans </dev/tty || ans=""
  ans="$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]')"
  [ -z "$ans" ] && ans="$default"
  [ "$ans" = "y" ] || [ "$ans" = "yes" ]
}

if [ -z "$EXTRAS_ARG" ] && [ "$WITH_RERANK" -eq 0 ] && [ "$WITH_HYBRID" -eq 0 ] && [ "$WITH_DOTNET" -eq 0 ]; then
  # Only interactive if stdin/stdout are a TTY.
  if [ -t 0 ] && [ -t 1 ]; then
    step "Optional extras"
    echo "  [rerank]  cross-encoder rerank stage (sentence-transformers + torch, ~1.5 GB)."
    echo "            Auto-fires on Apple Silicon / CUDA; CPU stays on bi-encoder."
    echo "  [hybrid]  in-process BGE-M3 (FlagEmbedding) for dense+sparse retrieval (~2.3 GB)."
    echo "            Default backend is Ollama (warm across hooks); hybrid is opt-in."
    echo "  [dotnet]  .NET DLL metadata indexing (dnfile, ~200 KB)."
    echo "            Skip if no .csproj / .NET source in repos you ingest."
    echo
    if prompt_yes_no "Install [rerank] extra?" "y"; then WITH_RERANK=1; fi
    if prompt_yes_no "Install [hybrid] extra? (heavy)" "n"; then WITH_HYBRID=1; fi
    if prompt_yes_no "Install [dotnet] extra?" "n"; then WITH_DOTNET=1; fi
  fi
fi

EXTRAS="dev"
[ "$WITH_RERANK" -eq 1 ] && EXTRAS="${EXTRAS},rerank"
[ "$WITH_HYBRID" -eq 1 ] && EXTRAS="${EXTRAS},hybrid"
[ "$WITH_DOTNET" -eq 1 ] && EXTRAS="${EXTRAS},dotnet"

step "Installing code-memory (editable, extras: $EXTRAS)"
pip install -e ".[${EXTRAS}]"
ok "code-memory installed"

# ---------- 4. .env ----------
step "Configuring .env"
if [ ! -f ".env" ]; then
  cp .env.example .env
  ok "Copied .env.example -> .env"
else
  ok ".env already present (not overwritten)"
fi

# ---------- 5. docker infra ----------
if [ "$SKIP_DOCKER" -eq 0 ]; then
  step "Starting FalkorDB + Qdrant (docker compose)"
  docker compose -f docker/docker-compose.yml up -d
  ok "Containers up"
  printf "${DIM}  FalkorDB browser: http://localhost:3000\n  Qdrant dashboard: http://localhost:6333/dashboard${RST}\n"
else
  warn "Docker step skipped"
fi

# ---------- 6. ollama model ----------
if [ "$SKIP_OLLAMA" -eq 0 ]; then
  step "Pulling embedding model (bge-m3)"

  # Make sure the Ollama daemon is reachable before pulling.
  ensure_ollama_daemon() {
    # Fast path: API responds.
    if ollama list >/dev/null 2>&1; then return 0; fi

    OS_KERNEL="$(uname -s)"
    if [ "$OS_KERNEL" = "Darwin" ]; then
      # Cask install registers an .app — open it (no-op if already running).
      if [ -d "/Applications/Ollama.app" ]; then
        open -a Ollama >/dev/null 2>&1 || true
      else
        # Fall back to launching the CLI server in the background.
        nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
      fi
    else
      # Linux install script sets up systemd; nudge it if available, else background.
      if have systemctl && systemctl list-unit-files 2>/dev/null | grep -q '^ollama\.service'; then
        sudo systemctl start ollama >/dev/null 2>&1 || true
      else
        nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
      fi
    fi

    # Wait up to ~30s for the daemon to accept requests.
    for _ in $(seq 1 30); do
      if ollama list >/dev/null 2>&1; then return 0; fi
      sleep 1
    done
    return 1
  }

  if ensure_ollama_daemon; then
    if ollama list 2>/dev/null | awk '{print $1}' | grep -q '^bge-m3'; then
      ok "bge-m3 already present"
    else
      ollama pull bge-m3
      ok "bge-m3 pulled"
    fi

    # Optional: claim-extraction model (Graphiti-style user-prompt facts).
    # Off by default — runtime is gated on CLAIMS_EXTRACTION=true anyway,
    # so pulling 5+ GB without consent would be rude.
    if [ "$WITH_CLAIMS" -eq 0 ] && [ -t 0 ] && [ -t 1 ]; then
      if prompt_yes_no "Also pull gemma2:9b for user-claim extraction? (~5.4 GB)" "n"; then
        WITH_CLAIMS=1
      fi
    fi
    if [ "$WITH_CLAIMS" -eq 1 ]; then
      if ollama list 2>/dev/null | awk '{print $1}' | grep -q '^gemma2:9b$'; then
        ok "gemma2:9b already present"
      else
        ollama pull gemma2:9b \
          && ok "gemma2:9b pulled" \
          || warn "gemma2:9b pull failed (you can retry: ollama pull gemma2:9b)"
      fi
      printf "${DIM}  Enable claim extraction at runtime with:\n    export CLAIMS_EXTRACTION=true${RST}\n"
    fi
  else
    warn "Ollama daemon did not become reachable within 30s — skipping model pull."
    warn "  Start Ollama manually (open the app on macOS, or 'sudo systemctl start ollama' on Linux),"
    warn "  then run: ollama pull bge-m3"
    [ "$WITH_CLAIMS" -eq 1 ] && warn "  Also: ollama pull gemma2:9b   (for user-claim extraction)"
  fi
else
  warn "Ollama step skipped (remember to pull a model before ingesting)"
fi

# ---------- 7. smoke tests ----------
if [ "$SKIP_TESTS" -eq 0 ]; then
  step "Running smoke tests"
  pytest -q
  ok "Tests passed"
else
  warn "Tests skipped"
fi

# ---------- 8. harness plugins ----------
step "Agent harness plugins"

# Resolve which plugins to install.
#   PLUGINS_ARG="" → interactive (only if stdin is a TTY)
#   PLUGINS_ARG="none" → skip
#   PLUGINS_ARG="all" → both
#   PLUGINS_ARG="opencode,claudecode" → comma-separated whitelist
INSTALL_OPENCODE=0
INSTALL_CLAUDECODE=0

resolve_plugin_selection() {
  local raw="$1"
  if [ "$raw" = "none" ]; then return 0; fi
  if [ "$raw" = "all" ]; then
    INSTALL_OPENCODE=1
    INSTALL_CLAUDECODE=1
    return 0
  fi
  IFS=',' read -r -a parts <<< "$raw"
  for p in "${parts[@]}"; do
    case "$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
      opencode)   INSTALL_OPENCODE=1 ;;
      claudecode|claude|claude-code) INSTALL_CLAUDECODE=1 ;;
      "" ) ;;
      *) warn "unknown plugin '$p' (expected: opencode, claudecode, all, none)" ;;
    esac
  done
}

if [ -n "$PLUGINS_ARG" ]; then
  resolve_plugin_selection "$PLUGINS_ARG"
elif [ -t 0 ] && [ -t 1 ]; then
  echo "  Optional: install the code-memory agent-harness plugins."
  echo "  They make the backend ambient (auto-retrieve / auto-reingest / record)."
  echo
  if prompt_yes_no "Install OpenCode plugin?" "y"; then INSTALL_OPENCODE=1; fi
  if prompt_yes_no "Install Claude Code plugin?" "y"; then INSTALL_CLAUDECODE=1; fi
  if [ "$INSTALL_OPENCODE" -eq 1 ] || [ "$INSTALL_CLAUDECODE" -eq 1 ]; then
    if prompt_yes_no "Install project-local (./.opencode and ./.claude) instead of global?" "n"; then
      PLUGINS_SCOPE="project"
    fi
  fi
else
  warn "non-interactive shell and no --plugins=... given; skipping plugin step"
fi

# Build a flat string of flags rather than an array — bash 3.2 + `set -u`
# barfs on "${arr[@]}" when arr is empty.
plugin_flags=""
[ "$PLUGINS_SCOPE" = "project" ] && plugin_flags="$plugin_flags --project"
[ "$SKIP_MCP" -eq 1 ] && plugin_flags="$plugin_flags --no-mcp"

if [ "$INSTALL_OPENCODE" -eq 1 ]; then
  if [ -x "$PROJECT_ROOT/plugins/opencode/install.sh" ]; then
    # shellcheck disable=SC2086 # intentional word-splitting on flag string
    "$PROJECT_ROOT/plugins/opencode/install.sh" $plugin_flags
    ok "OpenCode plugin installed ($PLUGINS_SCOPE)"
  else
    warn "plugins/opencode/install.sh not executable; skipping"
  fi
fi

if [ "$INSTALL_CLAUDECODE" -eq 1 ]; then
  if [ -x "$PROJECT_ROOT/plugins/claude-code/install.sh" ]; then
    # shellcheck disable=SC2086 # intentional word-splitting on flag string
    "$PROJECT_ROOT/plugins/claude-code/install.sh" $plugin_flags
    ok "Claude Code plugin installed ($PLUGINS_SCOPE)"
  else
    warn "plugins/claude-code/install.sh not executable; skipping"
  fi
fi

if [ "$INSTALL_OPENCODE" -eq 0 ] && [ "$INSTALL_CLAUDECODE" -eq 0 ]; then
  warn "no harness plugin installed; re-run with --plugins=all (or =opencode/=claudecode) later"
fi

# ---------- done ----------
step "Done"
cat <<EOF

  Activate the virtualenv:
    source .venv/bin/activate

  Ingest a repo:
    code-memory ingest /path/to/repo

  Query memory:
    code-memory retrieve "where is the auth middleware?"

  Browse:
    FalkorDB  http://localhost:3000
    Qdrant    http://localhost:6333/dashboard

  Optional knobs (see .env.example):
    EMBED_BACKEND=flagembed    # opt-in in-process BGE-M3 (needs --with-hybrid)
    CODEMEMORY_HYBRID=1        # dense+sparse RRF (only with flagembed backend)
    CODEMEMORY_RERANK=auto     # CE rerank (needs --with-rerank; auto on MPS/CUDA)
    CLAIMS_EXTRACTION=true     # Graphiti-style user-claim extraction
                               #   (needs --with-claims or `ollama pull gemma2:9b`)
    CLAIMS_LLM_MODEL=gemma2:9b # override the extraction model
EOF
