<#
.SYNOPSIS
  code-memory installer (Windows PowerShell)

.DESCRIPTION
  Sets up code-memory locally on Windows: checks Python/Docker/Ollama,
  creates a virtualenv, installs the package, starts Docker services,
  pulls the bge-m3 embedding model, runs the smoke tests, and optionally
  registers the OpenCode / Claude Code / Cursor harness plugins.

.PARAMETER NoDocker
  Skip the docker compose step.

.PARAMETER NoOllama
  Skip pulling the embedding model.

.PARAMETER NoTests
  Skip the pytest smoke run.

.PARAMETER NoMcp
  Skip the MCP server auto-registration (passed through to plugin installers).

.PARAMETER WithDotnet
  Install the [dotnet] extra (dnfile, ~200 KB). Enables .NET assembly
  metadata indexing (Assembly + Type graph nodes from .dll referenced
  via .csproj). Skip if no .NET source in repos you ingest.

.PARAMETER WithHybrid
  Install the [hybrid] extra (FlagEmbedding, ~2 GB torch). Enables in-process
  BGE-M3 dense+sparse hybrid reranking. Heavy — skip unless you need it.

.PARAMETER Extras
  Comma list of optional extras to install (e.g. 'dotnet,hybrid'), or 'none'
  to bypass the interactive prompt. Overrides -WithDotnet/-WithHybrid and the
  CODEMEMORY_EXTRAS environment variable.

.PARAMETER Plugins
  Comma-separated whitelist of harness plugins to install:
  'opencode', 'claudecode', 'cursor', 'all', or 'none'. Bypasses the
  interactive prompt. Default: interactive when stdin is a TTY, skip
  otherwise.

.PARAMETER PluginsScope
  'global' (default) installs under ~/.config/opencode, %APPDATA%/Claude,
  or ~/.cursor; 'project' installs into ./.opencode, ./.claude, or
  ./.cursor.

.PARAMETER ExtrasInteractive
  Set to `$false` to skip the interactive extras prompt entirely. Overridden
  by -Extras, -WithDotnet, -WithHybrid, and the CODEMEMORY_EXTRAS env var.

.EXAMPLE
  ./scripts/install.ps1
  ./scripts/install.ps1 -NoOllama
  ./scripts/install.ps1 -WithDotnet -Plugins all
  ./scripts/install.ps1 -WithHybrid
  ./scripts/install.ps1 -Extras dotnet,hybrid
  ./scripts/install.ps1 -Extras none
  ./scripts/install.ps1 -Plugins claudecode -PluginsScope project

.NOTES
  Environment variables:
    CODEMEMORY_EXTRAS=dotnet,hybrid   same as -Extras (overrides interactive prompt)
    CODEMEMORY_EXTRAS=none            skip extras entirely
#>

[CmdletBinding()]
param(
  [switch]$NoDocker,
  [switch]$NoOllama,
  [switch]$NoTests,
  [switch]$NoMcp,
  [switch]$WithDotnet,
  [switch]$WithHybrid,
  [string]$Extras = '',
  [string]$Plugins = '',
  [ValidateSet('global', 'project')]
  [string]$PluginsScope = 'global',
  [bool]$ExtrasInteractive = $true
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

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "[ok]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[warn] $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "[err]  $msg" -ForegroundColor Red; exit 1 }
function Dim($msg)  { Write-Host "  $msg" -ForegroundColor DarkGray }

function Test-Cmd($name) {
  return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Prompt-YesNo($question, $defaultYes) {
  $hint = if ($defaultYes) { '[Y/n]' } else { '[y/N]' }
  $ans = Read-Host "  $question $hint"
  if ([string]::IsNullOrWhiteSpace($ans)) {
    return $defaultYes
  }
  return ($ans.ToLower() -in @('y', 'yes'))
}

# ---------- docker resolution (Docker Desktop NOT required) ----------
# Any working engine is accepted, probed in order: `docker` on PATH with a
# live daemon (Docker Desktop, docker-ce, ...), then docker-ce inside the
# default WSL2 distro via `wsl -e docker`. Sets $script:DockerKind to
# native|wsl|daemon-down|none and returns the argv prefix array (or $null).
# No provisioning here — the zero-clone installer (root install.ps1) has it.
function Resolve-DockerCmd {
  $env:WSL_UTF8 = '1'
  if (Test-Cmd 'docker') {
    Invoke-NativeQuiet { docker info }
    if ($LASTEXITCODE -eq 0) { $script:DockerKind = 'native'; return ,@('docker') }
  }
  if (Test-Cmd 'wsl') {
    Invoke-NativeQuiet { wsl -e docker info }
    if ($LASTEXITCODE -eq 0) { $script:DockerKind = 'wsl'; return ,@('wsl','-e','docker') }
  }
  $script:DockerKind = if (Test-Cmd 'docker') { 'daemon-down' } else { 'none' }
  return $null
}

# Run docker through the resolved prefix: Invoke-Docker compose ... up -d
# Relative paths (docker/docker-compose.yml) work through WSL unchanged —
# wsl.exe starts in the /mnt/... equivalent of the Windows cwd.
function Invoke-Docker {
  param([Parameter(ValueFromRemainingArguments)]$Rest)
  $exe = $script:DockerCmd[0]
  $pre = @($script:DockerCmd | Select-Object -Skip 1)
  & $exe @pre @Rest
}

# Compose project label via plain-JSON `docker inspect` — not the Go template
# form, whose inner quotes are an argument-reparsing hazard through wsl.exe.
function Get-CmProjectLabel([string]$Name) {
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'SilentlyContinue'
  try {
    $json = (Invoke-Docker inspect $Name 2>$null) -join "`n"
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($json)) { return $null }
    return (ConvertFrom-Json $json)[0].Config.Labels.'com.docker.compose.project'
  } catch {
    return $null
  } finally {
    $ErrorActionPreference = $prevEAP
  }
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
  $script:DockerCmd = Resolve-DockerCmd
  if (-not $script:DockerCmd) {
    if ($script:DockerKind -eq 'daemon-down') {
      Die "Docker CLI found but no daemon reachable. Start it (Docker Desktop, or WSL2 docker-ce: wsl -e sudo systemctl start docker) and re-run."
    }
    Die "No working docker daemon found (tried 'docker' and 'wsl -e docker'). Any engine works — docker-ce in WSL2, Colima, Docker Desktop. Provision one via the one-liner installer (install.ps1) or README section 'Docker without Docker Desktop'."
  }
  Invoke-NativeQuiet { Invoke-Docker compose version }
  if ($LASTEXITCODE -ne 0)          { Die "Docker Compose v2 not available (need 'docker compose')." }
  Ok "Docker present$(if ($script:DockerKind -eq 'wsl') { ' (via WSL2)' })"
  if ($script:DockerKind -eq 'wsl') {
    # WSL2 idle-shutdown stops dockerd (and the containers) ~1 min after the
    # last session detaches. The one-liner installer sets up a hidden
    # Startup-folder keepalive; just warn here if it's missing.
    $vbs = Join-Path ([Environment]::GetFolderPath('Startup')) 'code-memory-wsl-docker.vbs'
    if (-not (Test-Path $vbs)) {
      Warn "no WSL keepalive found — dockerd will stop when WSL idles between sessions."
      Dim  "Install it via the one-liner installer (install.ps1), or see README section 'Docker without Docker Desktop'."
    }
  }
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

# uvx (from astral `uv`) — needed by the MCP server registration step.
$SkipMcpEffective = [bool]$NoMcp
if (-not $SkipMcpEffective) {
  if (Test-Cmd 'uvx') {
    Ok "uvx present"
  } else {
    Warn "uvx not found on PATH (provides the MCP server entrypoint)."
    if (Test-Cmd 'winget') {
      Dim "winget can install it: winget install --id=astral-sh.uv -e"
    } elseif (Test-Cmd 'pipx') {
      Dim "pipx can install it:   pipx install uv"
    } elseif (Test-Cmd 'powershell') {
      Dim "PowerShell one-liner:   irm https://astral.sh/uv/install.ps1 | iex"
    } else {
      Warn "No installer detected (winget / pipx / powershell). Install uv manually, then re-run."
      $SkipMcpEffective = $true
    }
    if (-not $SkipMcpEffective) {
      Dim "Plugin installers will attempt to install uv automatically if missing."
    }
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
# Resolve optional extras up front so we do one pip resolve.
#
# Optional extras — keep in sync with EXTRAS registry in updater.py.
#   dotnet: .NET assembly metadata indexing (dnfile, ~200 KB).
#   hybrid: in-process BGE-M3 dense+sparse via FlagEmbedding (~2 GB torch).
#
# Priority: -Extras / -WithDotnet / -WithHybrid (CLI) >
#           CODEMEMORY_EXTRAS env var > interactive prompt > skip.
#
# $isTty: true when running interactively (not under irm|iex which redirects stdin).
$isTty = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected

$installDotnet = [bool]$WithDotnet
$installHybrid = [bool]$WithHybrid

# Fold legacy switches into -Extras when -Extras is not already set.
if ([string]::IsNullOrWhiteSpace($Extras)) {
  $legacyParts = @()
  if ($WithDotnet) { $legacyParts += 'dotnet' }
  if ($WithHybrid) { $legacyParts += 'hybrid' }
  if ($legacyParts.Count -gt 0) { $Extras = $legacyParts -join ',' }
}

# Determine effective source.
$effectiveExtras = $Extras
if ([string]::IsNullOrWhiteSpace($effectiveExtras)) {
  $envExtras = [System.Environment]::GetEnvironmentVariable('CODEMEMORY_EXTRAS')
  if (-not [string]::IsNullOrWhiteSpace($envExtras)) {
    $effectiveExtras = $envExtras.Trim()
  }
}

if (-not [string]::IsNullOrWhiteSpace($effectiveExtras)) {
  # Flag/env path — parse comma list.
  if ($effectiveExtras -ieq 'none') {
    $installDotnet = $false
    $installHybrid = $false
  } else {
    foreach ($ep in $effectiveExtras.Split(',')) {
      switch ($ep.Trim().ToLower()) {
        'dotnet' { $installDotnet = $true }
        'hybrid' { $installHybrid = $true }
        ''       { }
        default  { Warn "unknown extra '$($ep.Trim())' (known: dotnet, hybrid) — skipped" }
      }
    }
  }
} elseif ($ExtrasInteractive -and $isTty) {
  # Interactive TTY path.
  Step "Optional extras"
  # Keep descriptions in sync with updater.py EXTRAS registry.
  Write-Host "  [dotnet]  .NET assembly metadata indexing (dnfile, ~200 KB)."
  Write-Host "            Skip if no .csproj / .NET source in repos you ingest."
  Write-Host ""
  Write-Host "  [hybrid]  In-process BGE-M3 dense+sparse via FlagEmbedding."
  Write-Host "            Heavy — pulls torch (~2 GB). Skip unless you need hybrid reranking."
  Write-Host ""
  if (Prompt-YesNo "Install [dotnet] extra?" $false) { $installDotnet = $true }
  if (Prompt-YesNo "Install [hybrid] extra?" $false) { $installHybrid = $true }
} else {
  # Non-interactive, no env override — dim hint only, no install.
  Dim "hint: optional extras not installed. Re-run with -Extras dotnet,hybrid or set CODEMEMORY_EXTRAS=dotnet,hybrid."
}

$extrasBase = @('dev')
if ($installDotnet) { $extrasBase += 'dotnet' }
if ($installHybrid) { $extrasBase += 'hybrid' }
$extrasJoined = ($extrasBase -join ',')

Step "Installing code-memory (editable, extras: $extrasJoined)"
# Base [dev] is hard-fail. Optional extras are non-fatal — a missing build
# dependency (e.g. torch wheel unavailable) must not abort the whole install.
& pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { Die "pip install failed" }
if ($installDotnet -or $installHybrid) {
  $optionalParts = @()
  if ($installDotnet) { $optionalParts += 'dotnet' }
  if ($installHybrid) { $optionalParts += 'hybrid' }
  $optionalJoined = $optionalParts -join ','
  & pip install -e ".[dev,$optionalJoined]"
  if ($LASTEXITCODE -ne 0) {
    Warn "optional extras install failed (dev install succeeded; re-run: pip install -e '.[$optionalJoined]')"
  }
}
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
  # Pin an explicit project name. The compose file uses fixed container_names
  # (cm-falkordb, ...), so they are global singletons: a later `compose up`
  # under a different project name collides with "container name already in
  # use". Reuse whatever project already owns the running containers (so their
  # data volumes, namespaced as <project>_falkor_data, stay attached); fall
  # back to a stable name for fresh installs. This keeps install and
  # `code-memory update` on one project without ever orphaning indexed data.
  $CmProject = Get-CmProjectLabel 'cm-falkordb'
  if (-not $CmProject) { $CmProject = Get-CmProjectLabel 'cm-qdrant' }
  if (-not $CmProject) { $CmProject = "code-memory" }
  $CmProject = "$CmProject".Trim()
  Invoke-Docker compose -p $CmProject -f docker/docker-compose.yml up -d --remove-orphans
  if ($LASTEXITCODE -ne 0) {
    Warn "compose up hit a container-name conflict — removing stale cm-* containers and retrying (named volumes persist)"
    Invoke-NativeQuiet { Invoke-Docker rm -f cm-falkordb cm-qdrant cm-tei }
    Invoke-Docker compose -p $CmProject -f docker/docker-compose.yml up -d --remove-orphans
    if ($LASTEXITCODE -ne 0) { Die "docker compose up failed" }
  }
  Ok "Containers up (project: $CmProject)"
  Dim "FalkorDB browser: http://localhost:3000"
  Dim "Qdrant dashboard: http://localhost:6333/dashboard"
} else {
  Warn "Docker step skipped"
}

# ---------- 6. ollama model ----------
if (-not $NoOllama) {
  Step "Pulling embedding model (bge-m3)"

  $daemonReady = $false
  Invoke-NativeQuiet { ollama list }
  if ($LASTEXITCODE -eq 0) { $daemonReady = $true }

  if (-not $daemonReady) {
    try {
      Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden -ErrorAction Stop
    } catch {
      Warn "Failed to start Ollama service: $($_.Exception.Message)"
    }

    for ($i = 0; $i -lt 30; $i++) {
      Start-Sleep -Seconds 1
      Invoke-NativeQuiet { ollama list }
      if ($LASTEXITCODE -eq 0) { $daemonReady = $true; break }
    }
  }

  if ($daemonReady) {
    $models = & ollama list 2>$null
    if ($models -match '^bge-m3') {
      Ok "bge-m3 already present"
    } else {
      & ollama pull bge-m3
      if ($LASTEXITCODE -ne 0) { Warn "ollama pull bge-m3 returned exit $LASTEXITCODE" } else { Ok "bge-m3 pulled" }
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

# ---------- 8. harness plugins ----------
Step "Agent harness plugins"

$installOpencode   = $false
$installClaudecode = $false
$installCursor     = $false
$installVibe       = $false

function Resolve-PluginSelection([string]$raw) {
  if ([string]::IsNullOrWhiteSpace($raw)) { return }
  if ($raw -ieq 'none') { return }
  if ($raw -ieq 'all') {
    $script:installOpencode = $true
    $script:installClaudecode = $true
    $script:installCursor = $true
    $script:installVibe = $true
    return
  }
  foreach ($p in $raw.Split(',')) {
    $key = $p.Trim().ToLower()
    switch ($key) {
      'opencode'     { $script:installOpencode = $true }
      'claudecode'   { $script:installClaudecode = $true }
      'claude'       { $script:installClaudecode = $true }
      'claude-code'  { $script:installClaudecode = $true }
      'cursor'       { $script:installCursor = $true }
      'vibe'         { $script:installVibe = $true }
      'mistral'      { $script:installVibe = $true }
      'mistral-vibe' { $script:installVibe = $true }
      ''             { }
      default        { Warn "unknown plugin '$p' (expected: opencode, claudecode, cursor, vibe, all, none)" }
    }
  }
}

if (-not [string]::IsNullOrWhiteSpace($Plugins)) {
  Resolve-PluginSelection $Plugins
} elseif ([Environment]::UserInteractive -and [Console]::IsInputRedirected -eq $false) {
  Write-Host "  Optional: install the code-memory agent-harness plugins."
  Write-Host "  They make the backend ambient (steering, auto-reingest, episode record)."
  Write-Host ""
  if (Prompt-YesNo "Install OpenCode plugin?" $true)    { $installOpencode = $true }
  if (Prompt-YesNo "Install Claude Code plugin?" $true) { $installClaudecode = $true }
  if (Prompt-YesNo "Install Cursor plugin?" $true)      { $installCursor = $true }
  if (Prompt-YesNo "Install Mistral Vibe plugin?" $true) { $installVibe = $true }
  if (($installOpencode -or $installClaudecode -or $installCursor -or $installVibe) -and $PluginsScope -eq 'global') {
    if (Prompt-YesNo "Install project-local (./.opencode, ./.claude, ./.cursor, ./.vibe) instead of global?" $false) {
      $PluginsScope = 'project'
    }
  }
} else {
  Warn "non-interactive shell and no -Plugins given; skipping plugin step"
}

function Invoke-PluginInstaller([string]$relativeScript, [string]$label, [string]$scopeStyle = 'project') {
  # scopeStyle: 'project' → `--project` (opencode), 'scope' → `--scope project` (claude-code, cursor)
  $scriptPath = Join-Path $projectRoot $relativeScript
  if (-not (Test-Path $scriptPath)) {
    Warn "$relativeScript not found; skipping $label"
    return
  }
  $extension = [System.IO.Path]::GetExtension($scriptPath).ToLower()
  $pluginArgs = @()
  if ($PluginsScope -eq 'project') {
    if ($scopeStyle -eq 'scope') { $pluginArgs += '--scope'; $pluginArgs += 'project' }
    else                          { $pluginArgs += '--project' }
  }
  if ($SkipMcpEffective)           { $pluginArgs += '--no-mcp' }

  if ($extension -eq '.ps1') {
    & $scriptPath @pluginArgs
  } elseif (Test-Cmd 'bash') {
    & bash $scriptPath @pluginArgs
  } else {
    Warn "Neither a .ps1 plugin installer nor bash is available for $label."
    Warn "  Install Git Bash or WSL to run the Unix plugin installers on Windows."
    return
  }
  if ($LASTEXITCODE -ne 0) {
    Warn "$label installer exited with code $LASTEXITCODE"
  } else {
    Ok "$label plugin installed ($PluginsScope)"
  }
}

if ($installOpencode) {
  Invoke-PluginInstaller 'plugins/opencode/install.sh' 'OpenCode'
}
if ($installClaudecode) {
  Invoke-PluginInstaller 'plugins/claude-code/install.sh' 'Claude Code' 'scope'
}
if ($installCursor) {
  Invoke-PluginInstaller 'plugins/cursor/install.sh' 'Cursor' 'scope'
}
if ($installVibe) {
  Invoke-PluginInstaller 'plugins/vibe/install.sh' 'Mistral Vibe' 'scope'
}
if (-not $installOpencode -and -not $installClaudecode -and -not $installCursor -and -not $installVibe) {
  Warn "no harness plugin installed; re-run with -Plugins all (or =opencode/=claudecode/=cursor/=vibe) later"
}

# ---------- done ----------
Step "Done"
$doneLines = @(
  ''
  '  Activate the virtualenv:'
  '    . .venv\Scripts\Activate.ps1'
  ''
  '  Ingest a repo:'
  '    code-memory ingest C:\path\to\repo'
  ''
  '  Query memory:'
  '    code-memory retrieve "where is the auth middleware?"'
  ''
  '  Browse:'
  '    FalkorDB  http://localhost:3000'
  '    Qdrant    http://localhost:6333/dashboard'
  ''
)
Write-Host ($doneLines -join [Environment]::NewLine)
