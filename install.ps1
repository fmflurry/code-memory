<#
.SYNOPSIS
  code-memory zero-clone installer (Windows PowerShell).

.DESCRIPTION
  No `git clone` required. Installs the `code-memory` CLI via `uv`, drops
  infra files into $HOME\.code-memory\, starts FalkorDB + Qdrant on any
  working docker engine — native (Docker Desktop, ...) or docker-ce inside
  WSL2 via `wsl -e docker`, offering to provision the latter when nothing
  is found (Docker Desktop NOT required) — pulls the bge-m3 embedding model
  (and optionally gemma2:9b for claim extraction), and wires up the Claude
  Code plugin + MCP server. Optionally installs the OpenCode plugin from npm.

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

# ---------- docker resolution (Docker Desktop NOT required) ----------
# Any working engine is accepted, probed in order:
#   1. `docker` on PATH with a live daemon (Docker Desktop, docker-ce, ...)
#   2. docker-ce inside the default WSL2 distro, reached via `wsl -e docker`
# Sets $script:DockerKind to native|wsl|daemon-down|none and returns the argv
# prefix array (or $null). WSL_UTF8=1 keeps wsl.exe diagnostics decodable
# (they are UTF-16LE by default).
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

# Translate a Windows path for the resolved docker; identity when native.
# Relative paths resolve against the shared cwd (WSL mirrors it under /mnt),
# so only absolute paths need wslpath.
function ConvertTo-DockerPath([string]$WinPath) {
  if ($script:DockerKind -ne 'wsl') { return $WinPath }
  if (-not [System.IO.Path]::IsPathRooted($WinPath)) { return ($WinPath -replace '\\','/') }
  $p = (& wsl -e wslpath -a "$WinPath" 2>$null)
  if ($LASTEXITCODE -eq 0 -and $p) { return "$p".Trim() }
  return '/mnt/' + $WinPath.Substring(0,1).ToLower() + ($WinPath.Substring(2) -replace '\\','/')
}

# Run docker through the resolved prefix: Invoke-Docker compose ... up -d
function Invoke-Docker {
  param([Parameter(ValueFromRemainingArguments)]$Rest)
  $exe = $script:DockerCmd[0]
  $pre = @($script:DockerCmd | Select-Object -Skip 1)
  & $exe @pre @Rest
}

# Provision docker-ce inside the default WSL2 distro — the Desktop-free path.
# Every mutating step asks first. Returns $true once `wsl -e docker info` works.
function Install-DockerInWsl {
  if (-not (Test-Interactive)) {
    Warn "non-interactive: skipping docker-ce provisioning. Manual steps:"
    Dim  "  wsl --install                 (elevated terminal, reboot, create your Linux user)"
    Dim  "  wsl -u root sh -c 'curl -fsSL https://get.docker.com | sh'"
    Dim  "  wsl -u root sh -c 'usermod -aG docker <you>; systemctl enable --now docker'"
    return $false
  }

  # 1. A WSL distro must exist and boot.
  Invoke-NativeQuiet { wsl -e true }
  while ($LASTEXITCODE -ne 0) {
    Warn "WSL is not ready (not installed, or no distro yet)."
    Dim  "In an ELEVATED terminal run:  wsl --install"
    Dim  "Reboot, let the distro create your Linux user, then come back here."
    Write-Host "? Press Enter to re-check (or 'skip'): " -ForegroundColor Yellow -NoNewline
    $ans = Read-Host
    if ($ans -eq 'skip') { return $false }
    Invoke-NativeQuiet { wsl -e true }
  }

  # 2. docker-ce needs WSL2 (real kernel), not WSL1.
  $kernel = ((& wsl -e uname -r 2>$null) -join '').Trim()
  if ($kernel -notmatch 'microsoft-standard|WSL2') {
    Warn "Default distro looks like WSL1 (kernel: $kernel); docker-ce needs WSL2."
    Dim  "Convert it:  wsl --set-version <distro> 2   then re-run this installer."
    return $false
  }

  # 3. systemd so dockerd starts with the distro.
  Invoke-NativeQuiet { wsl -e test -d /run/systemd/system }
  if ($LASTEXITCODE -ne 0) {
    Warn "systemd is not enabled in the distro (needed so dockerd starts automatically)."
    if (-not (Ask-YesNo "Enable systemd in /etc/wsl.conf? This runs 'wsl --shutdown', terminating ALL running WSL sessions" "Y")) { return $false }
    & wsl -u root sh -c "grep -qs '^systemd=true' /etc/wsl.conf || printf '[boot]\nsystemd=true\n' >> /etc/wsl.conf"
    & wsl --shutdown
    Invoke-NativeQuiet { wsl -e test -d /run/systemd/system }
    if ($LASTEXITCODE -ne 0) { Warn "systemd still not active after restart — aborting provisioning"; return $false }
    Ok "systemd enabled"
  }

  # 4. docker-ce via the official convenience script.
  Invoke-NativeQuiet { wsl -e docker --version }
  if ($LASTEXITCODE -ne 0) {
    if (-not (Ask-YesNo "Install docker-ce inside the distro via get.docker.com?" "Y")) { return $false }
    Invoke-NativeVisible { wsl -u root sh -c "curl -fsSL https://get.docker.com | sh" }
    if ($LASTEXITCODE -ne 0) {
      Warn "docker-ce install failed."
      Dim  "Corporate proxy? Set HTTP_PROXY/HTTPS_PROXY inside the distro and re-run."
      return $false
    }
  } else {
    Ok "docker CLI already present in the distro"
  }

  # 5. Non-root access + start on boot. Terminate so group membership applies.
  $wu = ((& wsl -e whoami) -join '').Trim()
  Invoke-NativeVisible { wsl -u root sh -c "usermod -aG docker $wu; systemctl enable --now docker" }
  $distro = ((& wsl -l -q) | Where-Object { $_ -and "$_".Trim() } | Select-Object -First 1)
  if ($distro) { Invoke-NativeQuiet { wsl --terminate "$distro".Trim() } }

  # 6. Verify end-to-end.
  Invoke-NativeQuiet { wsl -e docker info }
  if ($LASTEXITCODE -ne 0) { Warn "docker daemon still not reachable via 'wsl -e docker'"; return $false }
  Ok "docker-ce running inside WSL2"

  # 7. WSL2 shuts the VM down ~1 min after the last session detaches — even
  # with systemd services running — taking dockerd and the containers with
  # it. A logon task holding a persistent session (`sleep infinity`, hidden
  # via powershell) keeps the VM alive; restart:unless-stopped brings the
  # containers back whenever dockerd (re)starts.
  if (Ask-YesNo "Create a logon task that keeps WSL (and dockerd) running in the background?" "Y") {
    & schtasks /Create /F /SC ONLOGON /TN "code-memory-wsl-docker" /TR "powershell.exe -NoProfile -WindowStyle Hidden -Command `"wsl.exe -e sleep infinity`"" | Out-Null
    if ($LASTEXITCODE -eq 0) {
      Ok "scheduled task 'code-memory-wsl-docker' created"
      Dim "remove later with:  schtasks /Delete /TN code-memory-wsl-docker /F"
      # Cover the current session too — the task only fires at next logon.
      Start-Process -FilePath 'wsl.exe' -ArgumentList '-e','sleep','infinity' -WindowStyle Hidden
    } else {
      Warn "could not create the scheduled task"
    }
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
  $script:DockerCmd = Resolve-DockerCmd

  if (-not $script:DockerCmd -and $script:DockerKind -eq 'daemon-down') {
    Warn "Docker CLI found but no daemon reachable."
    Dim  "Start it: Docker Desktop if that's what you use, or (WSL2 docker-ce)  wsl -e sudo systemctl start docker"
    if (Test-Interactive) {
      Write-Host "? Press Enter once the daemon is up (or 'skip'): " -ForegroundColor Yellow -NoNewline
      $ans = Read-Host
      if ($ans -ne 'skip') { $script:DockerCmd = Resolve-DockerCmd }
    }
  }

  if (-not $script:DockerCmd -and $script:DockerKind -eq 'none') {
    Warn "No docker found (native or WSL). Docker Desktop is NOT required."
    if (Ask-YesNo "Provision docker-ce inside your WSL2 distro now? (recommended)" "Y") {
      if (Install-DockerInWsl) { $script:DockerCmd = Resolve-DockerCmd }
    }
    if (-not $script:DockerCmd) {
      Dim "Options:"
      Dim "  a) WSL2 + docker-ce:  wsl --install  (elevated, reboot)  then re-run this installer"
      Dim "  b) any other docker engine (Docker Desktop, Rancher Desktop, ...), then re-run"
    }
  }

  if ($script:DockerCmd) {
    $composeArg = ConvertTo-DockerPath (Join-Path $HomeDir 'docker/docker-compose.yml')
    $projArg    = ConvertTo-DockerPath $HomeDir
    Invoke-NativeVisible { Invoke-Docker compose -f $composeArg --project-directory $projArg -p code-memory up -d }
    if ($LASTEXITCODE -ne 0) {
      Warn "docker compose up failed"
    } else {
      Ok "containers up$(if ($script:DockerKind -eq 'wsl') { ' (via WSL2)' })"
      if ($script:DockerKind -eq 'wsl') {
        # Ports published inside WSL2 must reach Windows through localhost
        # forwarding — verify end-to-end instead of assuming.
        $reachable = $false
        foreach ($i in 1..5) {
          try {
            Invoke-WebRequest -Uri 'http://127.0.0.1:6333/readyz' -TimeoutSec 5 -UseBasicParsing | Out-Null
            $reachable = $true; break
          } catch { Start-Sleep -Seconds 2 }
        }
        if ($reachable) {
          Ok "Qdrant reachable from Windows at 127.0.0.1:6333"
        } else {
          Warn "Containers are up in WSL but 127.0.0.1:6333 is not reachable from Windows."
          Dim  "Check %USERPROFILE%\.wslconfig: localhostForwarding must not be false;"
          Dim  "with networkingMode=mirrored, make sure the host firewall allows the ports."
        }
      }
      Dim "FalkorDB browser: http://localhost:3000"
      Dim "Qdrant dashboard: http://localhost:6333/dashboard"
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
$doneLines = @(
  ''
  "  Side files:    $HomeDir\"
  "  CLI:           $(if ($cliPath) { $cliPath.Source } else { 'code-memory (not on PATH)' })"
  ''
  '  Ingest a repo:'
  '    code-memory ingest C:\path\to\repo'
  ''
  '  Query:'
  '    code-memory retrieve "where is the auth middleware?"'
  ''
  '  Browse:'
  '    FalkorDB  http://localhost:3000'
  '    Qdrant    http://localhost:6333/dashboard'
  ''
  "  Edit defaults: $HomeDir\.env"
)
Write-Host ($doneLines -join [Environment]::NewLine)
