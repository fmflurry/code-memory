#!/usr/bin/env bash
#
# code-memory installer (macOS / Linux)
#
# Usage:
#   ./scripts/install.sh                 # full install
#   ./scripts/install.sh --no-docker     # skip docker compose
#   ./scripts/install.sh --no-ollama     # skip ollama pull
#   ./scripts/install.sh --no-tests      # skip smoke tests
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
for arg in "$@"; do
  case "$arg" in
    --no-docker) SKIP_DOCKER=1 ;;
    --no-ollama) SKIP_OLLAMA=1 ;;
    --no-tests)  SKIP_TESTS=1 ;;
    -h|--help)
      sed -n '1,12p' "$0"; exit 0 ;;
    *) die "unknown flag: $arg" ;;
  esac
done

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
