<#
.SYNOPSIS
  code-memory zero-clone installer (Windows PowerShell).

.DESCRIPTION
  No `git clone` required. Installs the `code-memory` CLI via `uv`, drops
  infra files into $HOME\.code-memory\, starts FalkorDB + Qdrant via Docker,
  pulls the bge-m3 embedding model, and wires up the Claude Code marketplace
  + plugin + MCP server. Optionally installs the OpenCode plugin from npm
  and runs its post-install registrar.

  One-liner:
    irm https://raw.githubusercontent.com/fmflurry/code-memory/main/install.ps1 | iex

  Contributors hacking on the repo should still `git clone` and run
  `scripts/install.ps1` instead.

.PARAMETER NoDocker
  Skip starting FalkorDB + Qdrant via docker compose.

.PARAMETER NoOllama
  Skip pulling bge-m3.

.PARAMETER NoClaude
  Skip Claude Code marketplace + plugin registration.

.PARAMETER NoOpencode
  Skip OpenCode plugin install from npm.

.PARAMETER NoMcp
  Skip MCP server registration with Claude Code.

.EXAMPLE
  irm https://raw.githubusercontent.com/fmflurry/code-memory/main/install.ps1 | iex

.EXAMPLE
  # Download then run with flags:
  iwr https://raw.githubusercontent.com/fmflurry/code-memory/main/install.ps1 -OutFile install.ps1
  ./install.ps1 -NoOpencode -NoOllama
#>

[CmdletBinding()]
param(
  [switch]$NoDocker,
  [switch]$NoOllama,
  [switch]$NoClaude,
  [switch]$NoOpencode,
  [switch]$NoMcp
)

$ErrorActionPreference = 'Stop'

$RepoUrl  = if ($env:CODEMEMORY_REPO_URL)  { $env:CODEMEMORY_REPO_URL }  else { 'https://github.com/fmflurry/code-memory' }
$RawUrl   = if ($env:CODEMEMORY_RAW_URL)   { $env:CODEMEMORY_RAW_URL }   else { 'https://raw.githubusercontent.com/fmflurry/code-memory/main' }
$HomeDir  = if ($env:CODEMEMORY_HOME)      { $env:CODEMEMORY_HOME }      else { Join-Path $HOME '.code-memory' }
$NpmPkg   = if ($env:CODEMEMORY_OPENCODE_PKG) { $env:CODEMEMORY_OPENCODE_PKG } else { 'code-memory-opencode' }

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "[ok]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[warn] $msg" -ForegroundColor Yellow }
function Err($msg)  { Write-Host "[err]  $msg" -ForegroundColor Red }
function Dim($msg)  { Write-Host "  $msg" -ForegroundColor DarkGray }
function Test-Cmd($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

# ---------- 1. uv ----------
Step "Ensuring uv is installed"
if (Test-Cmd 'uv') {
  Ok "uv $(uv --version 2>$null)"
} else {
  # Run uv installer in a child powershell so its `exit` cannot tear down
  # the parent session (especially when this script itself was launched via
  # `irm | iex`). Also temporarily relax ErrorActionPreference so transient
  # non-terminating warnings from the uv installer don't kill us.
  $uvInstaller = Join-Path ([System.IO.Path]::GetTempPath()) ("uv-install-{0}.ps1" -f ([guid]::NewGuid()))
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile $uvInstaller -UseBasicParsing
    $psExe = (Get-Process -Id $PID).Path
    if (-not $psExe) { $psExe = 'powershell.exe' }
    & $psExe -NoProfile -ExecutionPolicy Bypass -File $uvInstaller
    $uvExit = $LASTEXITCODE
    if ($uvExit -ne 0) { throw "uv installer exited with code $uvExit" }
  } catch {
    Err "Failed to install uv: $($_.Exception.Message)"
    Err "Install manually:  winget install --id=astral-sh.uv -e   then re-run."
    exit 3
  } finally {
    Remove-Item $uvInstaller -ErrorAction SilentlyContinue
    $ErrorActionPreference = $prevEAP
  }
  $env:Path = "$HOME\.local\bin;$HOME\.cargo\bin;$env:Path"
  if (-not (Test-Cmd 'uv')) {
    Err "uv installed but not on PATH; re-open shell and re-run."
    exit 3
  }
  Ok "uv installed"
}

# ---------- 2. code-memory CLI ----------
Step "Installing code-memory CLI"
& uv tool install --force --from "git+$RepoUrl" code-memory
if ($LASTEXITCODE -ne 0) { Err "uv tool install failed"; exit 1 }
$cliPath = (Get-Command code-memory -ErrorAction SilentlyContinue)
Ok "code-memory CLI: $($cliPath.Source)"

# ---------- 3. side files ----------
Step "Writing infra files to $HomeDir"
New-Item -ItemType Directory -Force -Path (Join-Path $HomeDir 'docker') | Out-Null
Invoke-WebRequest -Uri "$RawUrl/docker/docker-compose.yml" -OutFile (Join-Path $HomeDir 'docker/docker-compose.yml') -UseBasicParsing
Ok "wrote $HomeDir\docker\docker-compose.yml"
$envFile = Join-Path $HomeDir '.env'
if (-not (Test-Path $envFile)) {
  Invoke-WebRequest -Uri "$RawUrl/.env.example" -OutFile $envFile -UseBasicParsing
  Ok "wrote $envFile (from .env.example)"
} else {
  Ok ".env already present (not overwritten)"
}

# ---------- 4. docker ----------
if (-not $NoDocker) {
  Step "Starting FalkorDB + Qdrant"
  if (-not (Test-Cmd 'docker')) {
    Warn "docker not found — install Docker Desktop and re-run (or pass -NoDocker)."
  } else {
    & docker compose -f (Join-Path $HomeDir 'docker/docker-compose.yml') --project-directory $HomeDir up -d
    if ($LASTEXITCODE -ne 0) { Warn "docker compose up failed" } else {
      Ok "containers up"
      Dim "FalkorDB browser: http://localhost:3000"
      Dim "Qdrant dashboard: http://localhost:6333/dashboard"
    }
  }
} else {
  Warn "docker step skipped"
}

# ---------- 5. ollama ----------
if (-not $NoOllama) {
  Step "Embedding model (bge-m3)"
  if (-not (Test-Cmd 'ollama')) {
    Warn "ollama not found. Install from https://ollama.com/download/windows, then: ollama pull bge-m3"
  } else {
    & ollama list 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
      try { Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden -ErrorAction Stop } catch {}
      for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        & ollama list 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
      }
    }
    $models = & ollama list 2>$null
    if ($models -match '^bge-m3') {
      Ok "bge-m3 already present"
    } else {
      & ollama pull bge-m3
      if ($LASTEXITCODE -eq 0) { Ok "bge-m3 pulled" } else { Warn "ollama pull returned exit $LASTEXITCODE" }
    }
  }
} else {
  Warn "ollama step skipped"
}

# ---------- 6. Claude Code ----------
if (-not $NoClaude) {
  if (-not (Test-Cmd 'claude')) {
    Warn "claude CLI not found — skipping Claude Code plugin"
  } else {
    Step "Registering Claude Code plugin + MCP"
    & claude plugin marketplace add $RepoUrl 2>$null
    if ($LASTEXITCODE -ne 0) { Warn "marketplace add failed (may already be registered)" }

    $pluginList = & claude plugin list 2>$null
    if ($pluginList -match 'code-memory@code-memory') {
      Ok "plugin already installed"
    } else {
      & claude plugin install code-memory@code-memory --scope user
      if ($LASTEXITCODE -eq 0) { Ok "plugin installed" } else { Warn "plugin install failed" }
    }

    if (-not $NoMcp) {
      $mcpList = & claude mcp list 2>$null
      if ($mcpList -match '^\s*code-memory\s') {
        Ok "MCP already registered"
      } else {
        & claude mcp add code-memory `
          --scope user `
          -e CODE_MEMORY_PROJECT=auto `
          -- uvx --from "git+$RepoUrl" code-memory-mcp
        if ($LASTEXITCODE -eq 0) { Ok "MCP registered (restart Claude Code to pick it up)" }
        else { Warn "claude mcp add failed; see README §MCP server" }
      }
    }
  }
}

# ---------- 7. OpenCode ----------
if (-not $NoOpencode) {
  Step "Installing OpenCode plugin"
  if (-not (Test-Cmd 'npm')) {
    Warn "npm not found — skipping. Install Node.js, then: npm i -g $NpmPkg ; code-memory-opencode-install"
  } else {
    & npm i -g $NpmPkg
    if ($LASTEXITCODE -ne 0) { Warn "npm install failed" }
    elseif (Test-Cmd 'code-memory-opencode-install') {
      & code-memory-opencode-install
    } else {
      Warn "$NpmPkg installed but code-memory-opencode-install not on PATH"
      Warn "Add npm global bin to PATH (npm bin -g) and re-run: code-memory-opencode-install"
    }
  }
}

# ---------- done ----------
Step "Done"
@"

  Side files:    $HomeDir\
  CLI:           $(if ($cliPath) { $cliPath.Source } else { 'code-memory (not on PATH)' })

  Ingest a repo:
    code-memory ingest C:\path\to\repo

  Query:
    code-memory retrieve "where is the auth middleware?"

  Browse:
    FalkorDB  http://localhost:3000
    Qdrant    http://localhost:6333/dashboard

  Edit defaults: $HomeDir\.env
"@ | Write-Host
