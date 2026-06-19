<#
.SYNOPSIS
  code-memory zero-clone installer (Windows PowerShell).

.DESCRIPTION
  No `git clone` required. Installs the `code-memory` CLI via `uv`, drops
  infra files into $HOME\.code-memory\, waits for Docker + Ollama,
  pulls the bge-m3 embedding model (and optionally gemma2:9b for claim
  extraction), and wires up the Claude Code plugin + MCP server.
  Optionally installs the OpenCode plugin from npm.

  Interactive by default. Pass -Yes to accept defaults; pass any -No*
  switch to skip a step; pass -NonInteractive to refuse all prompts.

  One-liner (interactive):
    irm https://raw.githubusercontent.com/fmflurry/code-memory/main/install.ps1 | iex

  Contributors hacking on the repo should still `git clone` and run
  `scripts/install.ps1` instead.

.PARAMETER Yes
  Accept default for every prompt (Claude=Y, OpenCode=N, claims=N).

.PARAMETER NonInteractive
  Refuse all prompts; use defaults (same as -Yes but without confirmation).

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

.PARAMETER NoClaims
  Skip pulling gemma2:9b for claim extraction.

.PARAMETER WithClaims
  Force-pull gemma2:9b without prompting.

.EXAMPLE
  irm https://raw.githubusercontent.com/fmflurry/code-memory/main/install.ps1 | iex

.EXAMPLE
  # Download then run with flags:
  iwr https://raw.githubusercontent.com/fmflurry/code-memory/main/install.ps1 -OutFile install.ps1
  ./install.ps1 -NoOpencode -NoOllama
#>

[CmdletBinding()]
param(
  [switch]$Yes,
  [switch]$NonInteractive,
  [switch]$NoDocker,
  [switch]$NoOllama,
  [switch]$NoClaude,
  [switch]$NoOpencode,
  [switch]$NoMcp,
  [switch]$NoClaims,
  [switch]$WithClaims
)

$ErrorActionPreference = 'Stop'
# PowerShell 7.3+ promotes native-command stderr to a terminating error when
# $ErrorActionPreference='Stop'. Docker CLI writes benign WARNINGs (e.g. the
# credential-plugin naming check) to stderr; we gate on $LASTEXITCODE instead,
# so do not let stderr alone abort the script.
$PSNativeCommandUseErrorActionPreference = $false

# Run a native command whose benign stderr (e.g. Docker CLI credential-plugin
# warnings) must NOT abort the script. Windows PowerShell 5.1 turns redirected
# native stderr into terminating NativeCommandError records under
# $ErrorActionPreference='Stop'; relaxing EAP for the call fixes it on 5.1 and
# 7+. Callers gate on $LASTEXITCODE. Returns nothing; sets $LASTEXITCODE.
function Invoke-NativeQuiet {
  param([Parameter(Mandatory)][scriptblock] $Command)
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'SilentlyContinue'
  try { & $Command 2>&1 | Out-Null } finally { $ErrorActionPreference = $prevEAP }
}
function Invoke-NativeVisible {
  param([Parameter(Mandatory)][scriptblock] $Command)
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $Command 2>&1 | ForEach-Object { Write-Host $_ } } finally { $ErrorActionPreference = $prevEAP }
}

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

function Test-Interactive() {
  if ($NonInteractive) { return $false }
  # Read-Host targets console host. When invoked via `irm | iex`, the
  # console host is still attached, so prompts work even though stdin is
  # the script body.
  try {
    return [Environment]::UserInteractive -and ($Host.Name -ne 'Default Host')
  } catch { return $false }
}

# Returns $true for yes, $false for no.
function Ask-YesNo([string]$Prompt, [string]$Default = 'Y') {
  $hint = if ($Default -match '^[Yy]') { '[Y/n]' } else { '[y/N]' }
  $defaultYes = $Default -match '^[Yy]'
  if ($Yes -or -not (Test-Interactive)) { return $defaultYes }
  Write-Host "? $Prompt $hint " -ForegroundColor Yellow -NoNewline
  $ans = Read-Host
  if ([string]::IsNullOrWhiteSpace($ans)) { return $defaultYes }
  return ($ans -match '^[Yy]')
}

# Waits until a CLI is on PATH. Returns $true if present, $false if user skipped.
function Wait-ForCmd([string]$Cmd, [string]$Label, [string]$Url) {
  while (-not (Test-Cmd $Cmd)) {
    Warn "$Label not found."
    Dim "Install from: $Url"
    if (-not (Test-Interactive)) {
      Warn "non-interactive: skipping $Label"
      return $false
    }
    Write-Host "? Press Enter once installed (or type 'skip' to skip): " -ForegroundColor Yellow -NoNewline
    $ans = Read-Host
    if ($ans -eq 'skip') { return $false }
    # Refresh PATH from machine + user so newly installed tools show up
    # without restarting the shell.
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + `
                [System.Environment]::GetEnvironmentVariable('Path','User')
  }
  return $true
}

# ---------- 1. uv ----------
Step "Ensuring uv is installed"
if (Test-Cmd 'uv') {
  Ok "uv $(uv --version 2>$null)"
} else {
  # Run uv installer in a child powershell so its `exit` cannot tear down
  # the parent session (especially when this script itself was launched via
  # `irm | iex`).
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
& uv tool install --force --from "git+$RepoUrl" flurryx-code-memory
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
$doDocker = -not $NoDocker
if ($doDocker -and -not $Yes -and (Test-Interactive)) {
  $doDocker = Ask-YesNo "Start FalkorDB + Qdrant via Docker?" "Y"
}
if ($doDocker) {
  Step "Starting FalkorDB + Qdrant"
  if (Wait-ForCmd 'docker' 'Docker Desktop' 'https://www.docker.com/products/docker-desktop') {
    # ensure daemon up
    Invoke-NativeQuiet { docker info }
    if ($LASTEXITCODE -ne 0) {
      Warn "Docker CLI present but daemon not running. Start Docker Desktop."
      if (Test-Interactive) {
        Write-Host "? Press Enter once the daemon is up (or 'skip'): " -ForegroundColor Yellow -NoNewline
        $ans = Read-Host
        if ($ans -eq 'skip') { $doDocker = $false }
      }
    }
    if ($doDocker) {
      Invoke-NativeVisible { docker compose -f (Join-Path $HomeDir 'docker/docker-compose.yml') --project-directory $HomeDir up -d }
      if ($LASTEXITCODE -ne 0) {
        Warn "docker compose up failed"
      } else {
        Ok "containers up"
        Dim "FalkorDB browser: http://localhost:3000"
        Dim "Qdrant dashboard: http://localhost:6333/dashboard"
      }
    }
  } else {
    Warn "docker step skipped"
  }
} else {
  Warn "docker step skipped"
}

# ---------- 5. ollama ----------
$doOllama = -not $NoOllama
if ($doOllama -and -not $Yes -and (Test-Interactive)) {
  $doOllama = Ask-YesNo "Pull embedding model via Ollama?" "Y"
}
if ($doOllama) {
  Step "Embedding model (bge-m3)"
  if (Wait-ForCmd 'ollama' 'Ollama' 'https://ollama.com/download/windows') {
    & ollama list 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
      try { Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden -ErrorAction Stop } catch {}
      for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        & ollama list 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
      }
    }
    $models = (& ollama list 2>$null) -join "`n"
    if ($models -match '(?m)^bge-m3(\s|:)') {
      Ok "bge-m3 already present"
    } else {
      & ollama pull bge-m3
      if ($LASTEXITCODE -eq 0) { Ok "bge-m3 pulled" } else { Warn "ollama pull bge-m3 returned exit $LASTEXITCODE" }
    }

    # optional gemma2:9b for claim extraction
    $doClaims = $false
    if ($WithClaims) {
      $doClaims = $true
    } elseif (-not $NoClaims) {
      $doClaims = Ask-YesNo "Also pull gemma2:9b for user-claim extraction (~5.4 GB)?" "N"
    }
    if ($doClaims) {
      $models2 = (& ollama list 2>$null) -join "`n"
      if ($models2 -match '(?m)^gemma2:9b\s') {
        Ok "gemma2:9b already present"
      } else {
        & ollama pull gemma2:9b
        if ($LASTEXITCODE -eq 0) { Ok "gemma2:9b pulled" } else { Warn "ollama pull gemma2:9b returned exit $LASTEXITCODE" }
      }
    }
  } else {
    Warn "ollama step skipped"
  }
} else {
  Warn "ollama step skipped"
}

# ---------- 6. Claude Code ----------
$doClaude = -not $NoClaude
if ($doClaude -and -not $Yes -and (Test-Interactive)) {
  $doClaude = Ask-YesNo "Install Claude Code plugin + MCP?" "Y"
}
if ($doClaude) {
  if (-not (Test-Cmd 'claude')) {
    Step "Installing Claude Code CLI"
    $claudeInstaller = Join-Path ([System.IO.Path]::GetTempPath()) ("claude-install-{0}.ps1" -f ([guid]::NewGuid()))
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
      Invoke-WebRequest -Uri 'https://claude.ai/install.ps1' -OutFile $claudeInstaller -UseBasicParsing
      $psExe = (Get-Process -Id $PID).Path
      if (-not $psExe) { $psExe = 'powershell.exe' }
      & $psExe -NoProfile -ExecutionPolicy Bypass -File $claudeInstaller
      if ($LASTEXITCODE -ne 0) { Warn "claude installer exited with code $LASTEXITCODE" }
    } catch {
      Warn "claude install failed: $($_.Exception.Message)"
    } finally {
      Remove-Item $claudeInstaller -ErrorAction SilentlyContinue
      $ErrorActionPreference = $prevEAP
    }
    # Refresh PATH from machine + user so the freshly installed claude shim is visible.
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + `
                [System.Environment]::GetEnvironmentVariable('Path','User') + ';' + `
                "$HOME\.local\bin"
  }
  if (-not (Test-Cmd 'claude')) {
    Warn "claude CLI still not found after install attempt — skipping Claude Code plugin"
    Dim "Install manually: https://docs.anthropic.com/claude/docs/claude-code"
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
      if ($mcpList -match '(?m)^\s*code-memory\s') {
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
} else {
  Warn "Claude Code step skipped"
}

# ---------- 7. OpenCode ----------
$doOpencode = -not $NoOpencode -and ($Yes -or $false)
# OpenCode defaults to N — only prompt if not explicitly skipped.
if (-not $NoOpencode -and -not $Yes -and (Test-Interactive)) {
  $doOpencode = Ask-YesNo "Install OpenCode plugin (npm global)?" "N"
} elseif ($Yes) {
  $doOpencode = $false  # -Yes accepts the default (N) for OpenCode
}
if ($doOpencode) {
  Step "Installing OpenCode plugin"
  if (-not (Test-Cmd 'npm')) {
    Warn "npm not found — skipping. Install Node.js, then: npm i -g $NpmPkg ; code-memory-opencode-install"
  } else {
    & npm i -g $NpmPkg
    if ($LASTEXITCODE -ne 0) {
      Warn "npm install failed"
    } elseif (Test-Cmd 'code-memory-opencode-install') {
      & code-memory-opencode-install
    } else {
      Warn "$NpmPkg installed but code-memory-opencode-install not on PATH"
      Warn "Add npm global bin to PATH (npm bin -g) and re-run: code-memory-opencode-install"
    }
  }
} else {
  Warn "OpenCode step skipped"
}

# ---------- done ----------
Step "Done"
$doneMessage = @"

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
"@
Write-Host $doneMessage
