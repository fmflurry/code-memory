#!/usr/bin/env bash
#
# code-memory installer (macOS / Linux)
#
# Usage:
#   ./scripts/install.sh                 # full install (interactive plugin prompt)
#   ./scripts/install.sh --no-docker     # skip docker compose
#   ./scripts/install.sh --no-ollama     # skip ollama pull
#   ./scripts/install.sh --no-tests      # skip smoke tests
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
PLUGINS_ARG=""       # empty = interactive; explicit value bypasses prompt
PLUGINS_SCOPE="global"  # global | project
for arg in "$@"; do
  case "$arg" in
    --no-docker) SKIP_DOCKER=1 ;;
    --no-ollama) SKIP_OLLAMA=1 ;;
    --no-tests)  SKIP_TESTS=1 ;;
    --plugins=*)        PLUGINS_ARG="${arg#--plugins=}" ;;
    --plugins-scope=*)  PLUGINS_SCOPE="${arg#--plugins-scope=}" ;;
    -h|--help)
      sed -n '1,18p' "$0"; exit 0 ;;
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
    warn "Ollama not found. Install from https://ollama.com/download, then re-run with --no-ollama or rerun this script."
    SKIP_OLLAMA=1
  else
    ok "Ollama $(ollama --version 2>/dev/null | head -1 | awk '{print $NF}')"
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
step "Installing code-memory (editable, with dev extras)"
pip install -e ".[dev]"
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
  if ollama list 2>/dev/null | awk '{print $1}' | grep -q '^bge-m3'; then
    ok "bge-m3 already present"
  else
    ollama pull bge-m3
    ok "bge-m3 pulled"
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

plugin_flag=""
[ "$PLUGINS_SCOPE" = "project" ] && plugin_flag="--project"

if [ "$INSTALL_OPENCODE" -eq 1 ]; then
  if [ -x "$PROJECT_ROOT/plugins/opencode/install.sh" ]; then
    # shellcheck disable=SC2086 # intentional word-splitting on a single flag
    "$PROJECT_ROOT/plugins/opencode/install.sh" $plugin_flag
    ok "OpenCode plugin installed ($PLUGINS_SCOPE)"
  else
    warn "plugins/opencode/install.sh not executable; skipping"
  fi
fi

if [ "$INSTALL_CLAUDECODE" -eq 1 ]; then
  if [ -x "$PROJECT_ROOT/plugins/claude-code/install.sh" ]; then
    # shellcheck disable=SC2086 # intentional word-splitting on a single flag
    "$PROJECT_ROOT/plugins/claude-code/install.sh" $plugin_flag
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
EOF
