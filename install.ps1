<#
.SYNOPSIS
    Installer for Coder - the offline AI coding assistant.
.DESCRIPTION
    Sets everything up so you can type `coder` in any project folder:
      1. Finds (or installs) a compatible Python 3.11 / 3.12
      2. Creates an isolated virtual environment and installs Coder into it
      3. Registers a global `coder` command on your PATH
      4. Ensures Ollama is installed, running, and has the required models
    Re-running is safe (idempotent).
.EXAMPLE
    ./install.ps1
#>
[CmdletBinding()]
param(
    # Skip the Ollama install / model-pull step (set up the CLI only).
    [switch]$NoOllama
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

# --- Config ---------------------------------------------------------------
$Root        = Split-Path -Parent $MyInvocation.MyCommand.Definition
$VenvDir     = Join-Path $Root '.venv'
$ShimDir     = Join-Path $env:LOCALAPPDATA 'Coder\bin'
$LlmModel    = 'qwen2.5-coder:7b'
$EmbedModel  = 'nomic-embed-text'
$PyOk        = @('3.11', '3.12')   # tree-sitter-languages ships no wheels above 3.12

function Info($m)  { Write-Host "  $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "  OK  $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  !   $m" -ForegroundColor Yellow }
function Die($m)   { Write-Host "  X   $m" -ForegroundColor Red; exit 1 }
function Step($m)  { Write-Host "`n==> $m" -ForegroundColor White }

Write-Host "`nCoder installer" -ForegroundColor Magenta
Write-Host "Repo: $Root`n"

# --- 1. Locate a compatible Python ---------------------------------------
Step "Locating Python ($($PyOk -join ' or '))"

function Test-PyVersion($exe, $prefixArgs) {
    try {
        $v = & $exe @prefixArgs -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $PyOk -contains $v) { return $v }
    } catch {}
    return $null
}

$PyCmd = $null   # array: exe + any prefix args (e.g. @('py','-3.12'))
# Prefer the py launcher with an explicit version.
foreach ($v in $PyOk) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        if (Test-PyVersion 'py' @("-$v")) { $PyCmd = @('py', "-$v"); break }
    }
}
# Fall back to python / python3 already on PATH.
if (-not $PyCmd) {
    foreach ($exe in 'python', 'python3') {
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            $v = Test-PyVersion $exe @()
            if ($v) { $PyCmd = @($exe); break }
        }
    }
}

# Not found: offer to install Python 3.12 (per-user, no admin).
if (-not $PyCmd) {
    Warn "No Python $($PyOk -join '/') found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Info "Installing Python 3.12 via winget..."
        winget install --id Python.Python.3.12 -e --source winget `
            --accept-package-agreements --accept-source-agreements
    } else {
        Info "Downloading the official Python 3.12 installer..."
        $ver = '3.12.10'
        $url = "https://www.python.org/ftp/python/$ver/python-$ver-amd64.exe"
        $exe = Join-Path $env:TEMP "python-$ver-amd64.exe"
        Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
        Info "Running silent per-user install..."
        Start-Process -FilePath $exe -Wait -ArgumentList `
            '/quiet','InstallAllUsers=0','PrependPath=0','Include_launcher=1','Include_pip=1'
    }
    # Re-detect via the py launcher.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        if (Test-PyVersion 'py' @('-3.12')) { $PyCmd = @('py', '-3.12') }
    }
    if (-not $PyCmd) {
        Die "Python was installed but not detected. Open a new terminal and re-run ./install.ps1"
    }
}
Ok "Using Python: $($PyCmd -join ' ')"

# --- 2. Create venv + install Coder --------------------------------------
Step "Creating virtual environment (.venv)"
if (Test-Path $VenvDir) {
    Info "Existing .venv found - recreating for a clean install."
    Remove-Item $VenvDir -Recurse -Force
}
& $PyCmd[0] @($PyCmd[1..($PyCmd.Count-1)]) -m venv $VenvDir
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path $VenvPy)) { Die "venv creation failed." }
Ok "venv ready"

Step "Installing Coder and dependencies (this can take a few minutes)"
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -e $Root
if ($LASTEXITCODE -ne 0) { Die "pip install failed." }
Ok "Coder installed into the venv"

# --- 3. Register a global `coder` command --------------------------------
Step "Registering the global 'coder' command"
New-Item -ItemType Directory -Force -Path $ShimDir | Out-Null
$shim = Join-Path $ShimDir 'coder.cmd'
$venvCoder = Join-Path $VenvDir 'Scripts\coder.exe'
# The shim just forwards to the venv entry point, preserving the caller's cwd.
"@echo off`r`n`"$venvCoder`" %*" | Set-Content -Path $shim -Encoding ASCII
Ok "Shim written: $shim"

# Add the shim dir to the user PATH (persistent) if missing.
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $ShimDir) {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$ShimDir", 'User')
    Ok "Added $ShimDir to your user PATH"
} else {
    Info "PATH already contains the shim dir"
}
# Make it usable in the current session too.
if (($env:Path -split ';') -notcontains $ShimDir) { $env:Path = "$env:Path;$ShimDir" }

# --- 4. Ollama + models ---------------------------------------------------
if (-not $NoOllama) {
    Step "Setting up Ollama"
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Warn "Ollama not found."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Info "Installing Ollama via winget..."
            winget install --id Ollama.Ollama -e --source winget `
                --accept-package-agreements --accept-source-agreements
            $env:Path = "$env:Path;$env:LOCALAPPDATA\Programs\Ollama"
        } else {
            Warn "winget unavailable. Install Ollama from https://ollama.com/download then re-run ./install.ps1"
        }
    }

    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        # Ensure the server is reachable; start it if not.
        function Test-Ollama {
            try { Invoke-RestMethod 'http://localhost:11434/api/tags' -TimeoutSec 3 | Out-Null; return $true }
            catch { return $false }
        }
        if (-not (Test-Ollama)) {
            Info "Starting the Ollama server..."
            Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden
            for ($i = 0; $i -lt 20 -and -not (Test-Ollama); $i++) { Start-Sleep 1 }
        }
        if (Test-Ollama) {
            Ok "Ollama is running"
            $have = (& ollama list) 2>$null
            foreach ($m in @($LlmModel, $EmbedModel)) {
                if ($have -match [regex]::Escape($m)) {
                    Info "Model already present: $m"
                } else {
                    Info "Pulling $m (this is a large download)..."
                    & ollama pull $m
                }
            }
            Ok "Models ready"
        } else {
            Warn "Could not reach Ollama. Start it manually ('ollama serve') then run: ollama pull $LlmModel; ollama pull $EmbedModel"
        }
    }
} else {
    Info "Skipping Ollama setup (-NoOllama)."
}

# --- Done -----------------------------------------------------------------
Write-Host "`n============================================================" -ForegroundColor Green
Write-Host " Coder is installed." -ForegroundColor Green
Write-Host "============================================================`n" -ForegroundColor Green
Write-Host " Open a NEW terminal, cd into any project, and run:" -ForegroundColor White
Write-Host "     coder`n" -ForegroundColor Cyan
Write-Host " (This terminal already has it on PATH for this session.)`n"
