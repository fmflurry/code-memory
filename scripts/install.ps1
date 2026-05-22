<#
.SYNOPSIS
  code-memory installer (Windows PowerShell)

.DESCRIPTION
  Sets up code-memory locally on Windows: checks Python/Docker/Ollama,
  creates a virtualenv, installs the package, starts Docker services,
  pulls the bge-m3 embedding model, and runs the smoke tests.

.PARAMETER NoDocker
  Skip the docker compose step.

.PARAMETER NoOllama
  Skip pulling the embedding model.

.PARAMETER NoTests
  Skip the pytest smoke run.

.EXAMPLE
  ./scripts/install.ps1
  ./scripts/install.ps1 -NoOllama
#>

[CmdletBinding()]
param(
  [switch]$NoDocker,
  [switch]$NoOllama,
  [switch]$NoTests
)

$ErrorActionPreference = 'Stop'

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "[ok]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[warn] $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "[err]  $msg" -ForegroundColor Red; exit 1 }

function Test-Cmd($name) {
  return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

$projectRoot = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $projectRoot

# ---------- 1. prereqs ----------
Step "Checking prerequisites"

$pythonBin = $null
foreach ($candidate in @('python', 'python3', 'py')) {
  if (Test-Cmd $candidate) {
    $verRaw = & $candidate -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
    if ($verRaw -match '^(\d+)\.(\d+)$') {
      $maj = [int]$Matches[1]; $min = [int]$Matches[2]
      if ($maj -ge 3 -and $min -ge 11) {
        $pythonBin = $candidate
        Ok "Python $verRaw ($candidate)"
        break
      }
    }
  }
}
if (-not $pythonBin) { Die "Python 3.11+ not found. Install from https://www.python.org/." }

if (-not $NoDocker) {
  if (-not (Test-Cmd 'docker'))     { Die "Docker not found. Install Docker Desktop: https://www.docker.com/." }
  & docker compose version *> $null
  if ($LASTEXITCODE -ne 0)          { Die "Docker Compose v2 not available (need 'docker compose')." }
  Ok "Docker present"
}

if (-not $NoOllama) {
  if (-not (Test-Cmd 'ollama')) {
    Warn "Ollama not found — attempting auto-install..."
    if (Test-Cmd 'winget') {
      & winget install --id Ollama.Ollama -e --silent --accept-source-agreements --accept-package-agreements
      if ($LASTEXITCODE -eq 0) { Ok "Ollama installed via winget" }
    } elseif (Test-Cmd 'choco') {
      & choco install ollama -y
      if ($LASTEXITCODE -eq 0) { Ok "Ollama installed via choco" }
    } else {
      Warn "Neither winget nor choco available. Download Ollama from https://ollama.com/download/windows"
    }
    if (-not (Test-Cmd 'ollama')) {
      Warn "Ollama still not on PATH after install attempt — skipping model pull."
      $NoOllama = $true
    } else {
      Ok "Ollama present"
    }
  } else {
    Ok "Ollama present"
  }
}

# ---------- 2. python venv ----------
Step "Creating Python virtual environment"
if (-not (Test-Path '.venv')) {
  & $pythonBin -m venv .venv
  Ok "Created .venv"
} else {
  Ok ".venv already exists"
}

$activate = Join-Path '.venv' 'Scripts/Activate.ps1'
. $activate
& python -m pip install --upgrade pip wheel | Out-Null
Ok "pip upgraded"

# ---------- 3. package install ----------
Step "Installing code-memory (editable, with dev extras)"
& pip install -e ".[dev]"
Ok "code-memory installed"

# ---------- 4. .env ----------
Step "Configuring .env"
if (-not (Test-Path '.env')) {
  Copy-Item '.env.example' '.env'
  Ok "Copied .env.example -> .env"
} else {
  Ok ".env already present (not overwritten)"
}

# ---------- 5. docker infra ----------
if (-not $NoDocker) {
  Step "Starting FalkorDB + Qdrant (docker compose)"
  & docker compose -f docker/docker-compose.yml up -d
  Ok "Containers up"
  Write-Host "  FalkorDB browser: http://localhost:3000" -ForegroundColor DarkGray
  Write-Host "  Qdrant dashboard: http://localhost:6333/dashboard" -ForegroundColor DarkGray
} else {
  Warn "Docker step skipped"
}

# ---------- 6. ollama model ----------
if (-not $NoOllama) {
  Step "Pulling embedding model (bge-m3)"

  # Make sure the daemon is reachable.
  $daemonReady = $false
  & ollama list *> $null
  if ($LASTEXITCODE -eq 0) { $daemonReady = $true }

  if (-not $daemonReady) {
    # Try to launch the Ollama service. The Windows installer registers a
    # background process; just kick it via Start-Process if it isn't running.
    try {
      Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden -ErrorAction Stop
    } catch {
      Warn "Failed to start Ollama service: $($_.Exception.Message)"
    }

    for ($i = 0; $i -lt 30; $i++) {
      Start-Sleep -Seconds 1
      & ollama list *> $null
      if ($LASTEXITCODE -eq 0) { $daemonReady = $true; break }
    }
  }

  if ($daemonReady) {
    $models = & ollama list 2>$null
    if ($models -match '^bge-m3') {
      Ok "bge-m3 already present"
    } else {
      & ollama pull bge-m3
      Ok "bge-m3 pulled"
    }
  } else {
    Warn "Ollama daemon did not become reachable within 30s — skipping model pull."
    Warn "  Start Ollama, then run: ollama pull bge-m3"
  }
} else {
  Warn "Ollama step skipped (remember to pull a model before ingesting)"
}

# ---------- 7. smoke tests ----------
if (-not $NoTests) {
  Step "Running smoke tests"
  & pytest -q
  if ($LASTEXITCODE -ne 0) { Die "Tests failed" }
  Ok "Tests passed"
} else {
  Warn "Tests skipped"
}

# ---------- done ----------
Step "Done"
@"

  Activate the virtualenv:
    . .venv\Scripts\Activate.ps1

  Ingest a repo:
    code-memory ingest C:\path\to\repo

  Query memory:
    code-memory retrieve "where is the auth middleware?"

  Browse:
    FalkorDB  http://localhost:3000
    Qdrant    http://localhost:6333/dashboard
"@ | Write-Host
