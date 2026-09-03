# Gamechanger Talker -- first-run setup.
#
# Installs everything the narrator needs on a clean Windows PC, once. The
# installer runs this after copying the files; the launcher runs it again only
# if it did not finish, so a second launch costs nothing.
#
# Deliberately idempotent, step by step. Every step checks whether it is
# already done before doing anything, because the two failure modes that
# matter here are "ran out of network halfway" and "user closed the window",
# and both are fixed by running it again rather than by starting over.
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Force   # redo everything

param(
    [switch]$Force,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x faster without it

$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Stamp = Join-Path $Root ".setup-complete"
$SetupVersion = "1"

function Say([string]$text, [string]$colour = "White") {
    Write-Host $text -ForegroundColor $colour
}
function Step([int]$n, [int]$of, [string]$text) {
    Write-Host ""
    Write-Host "[$n/$of] $text" -ForegroundColor Cyan
}
function Warn([string]$text) { Write-Host "  ! $text" -ForegroundColor Yellow }
function Good([string]$text) { Write-Host "  + $text" -ForegroundColor Green }

# Model tags come from config.toml rather than being repeated here, so that
# changing the model in config does not silently leave setup pulling the old
# one onto every new machine.
function Get-ConfigValue([string]$section, [string]$key, [string]$fallback) {
    $path = Join-Path $Root "config.toml"
    if (-not (Test-Path $path)) { return $fallback }
    $inSection = $false
    foreach ($line in Get-Content $path) {
        $t = $line.Trim()
        if ($t -match '^\[([^\]]+)\]') {
            $inSection = ($matches[1] -eq $section)
            continue
        }
        if ($inSection -and $t -match "^$key\s*=\s*`"([^`"]+)`"") {
            return $matches[1]
        }
    }
    return $fallback
}

function Test-Command([string]$name) {
    $null = Get-Command $name -ErrorAction SilentlyContinue
    return $?
}

function Get-Downloaded([string]$url, [string]$outFile, [string]$label) {
    if (Test-Path $outFile) {
        Good "$label already downloaded"
        return
    }
    Say "  downloading $label ..."
    Invoke-WebRequest -Uri $url -OutFile $outFile -UseBasicParsing
    Good "$label downloaded"
}

Write-Host ""
Write-Host "  Gamechanger Talker -- setup" -ForegroundColor White
Write-Host "  ---------------------------" -ForegroundColor DarkGray
Write-Host "  Installing to: $Root" -ForegroundColor DarkGray

if ((Test-Path $Stamp) -and -not $Force) {
    $done = (Get-Content $Stamp -Raw).Trim()
    if ($done -eq $SetupVersion) {
        Good "Setup already completed. Use -Force to run it again."
        if (-not $NoPause) { Write-Host ""; Read-Host "Press Enter to close" }
        exit 0
    }
}

Write-Host ""
Warn "This downloads roughly 15 GB the first time (PyTorch, the speech model,"
Warn "and two local AI models). It only happens once. Leave it running."

$total = 6

# ---------------------------------------------------------------------------
Step 1 $total "Checking this PC"
# ---------------------------------------------------------------------------
$gpu = ""
if (Test-Command "nvidia-smi") {
    try { $gpu = (& nvidia-smi --query-gpu=name --format=csv,noheader | Select-Object -First 1) } catch { $gpu = "" }
}
if ($gpu) {
    Good "NVIDIA GPU: $gpu"
    $cuda = $true
} else {
    $cuda = $false
    Warn "No NVIDIA GPU found. The speech engine will run on the CPU, which is"
    Warn "much slower, and the avatar lip-sync may not keep up. It will still run."
}
$ram = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Good "RAM: $ram GB"

# ---------------------------------------------------------------------------
Step 2 $total "Python 3.12"
# ---------------------------------------------------------------------------
# Not 3.13: the speech engine (kokoro) pins itself below it, and pip will
# refuse to install at all on 3.13. This is the single most common way a
# working machine turns into a broken one, so it is checked explicitly.
$py = ""
foreach ($candidate in @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:ProgramFiles\Python312\python.exe",
    "C:\Python312\python.exe"
)) {
    if (Test-Path $candidate) { $py = $candidate; break }
}
if (-not $py) {
    Say "  installing Python 3.12 ..."
    $ok = $false
    if (Test-Command "winget") {
        try {
            & winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements | Out-Null
            $ok = $true
        } catch { $ok = $false }
    }
    if (-not $ok) {
        $exe = Join-Path $env:TEMP "python-3.12-amd64.exe"
        Get-Downloaded "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" $exe "Python 3.12"
        Start-Process -FilePath $exe -ArgumentList "/quiet","InstallAllUsers=0","PrependPath=0","Include_pip=1" -Wait
    }
    foreach ($candidate in @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "C:\Python312\python.exe"
    )) {
        if (Test-Path $candidate) { $py = $candidate; break }
    }
}
if (-not $py) {
    Write-Host ""
    Say "Could not install Python 3.12 automatically." Red
    Say "Install it from https://www.python.org/downloads/release/python-31210/" Red
    Say "then run this setup again." Red
    if (-not $NoPause) { Read-Host "Press Enter to close" }
    exit 1
}
Good "Python: $py"

# ---------------------------------------------------------------------------
Step 3 $total "Virtual environment"
# ---------------------------------------------------------------------------
if ($Force -and (Test-Path $Venv)) {
    Say "  removing the old environment ..."
    Remove-Item $Venv -Recurse -Force
}
if (-not (Test-Path $Python)) {
    Say "  creating .venv ..."
    & $py -m venv $Venv
}
Good "environment ready"
& $Python -m pip install --upgrade pip --quiet

# ---------------------------------------------------------------------------
Step 4 $total "PyTorch and Python packages (~3 GB)"
# ---------------------------------------------------------------------------
$torchOk = $false
try {
    & $Python -c "import torch" 2>$null
    if ($LASTEXITCODE -eq 0) { $torchOk = $true }
} catch { $torchOk = $false }

if ($torchOk -and -not $Force) {
    Good "PyTorch already installed"
} else {
    if ($cuda) {
        # CUDA 12.8 specifically: the current NVIDIA cards (Blackwell, sm_120)
        # have no kernels in the default PyPI wheels. They import fine and then
        # die on the first kernel launch, an hour into a stream.
        Say "  installing PyTorch with CUDA 12.8 support ..."
        & $Python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
    } else {
        Say "  installing PyTorch (CPU build) ..."
        & $Python -m pip install torch torchaudio
    }
    if ($LASTEXITCODE -ne 0) { Warn "PyTorch install reported a problem; continuing" }
}

Say "  installing the rest of the packages ..."
& $Python -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Say "Package install failed. Check your internet connection and run setup again." Red
    if (-not $NoPause) { Read-Host "Press Enter to close" }
    exit 1
}
Good "packages installed"

# ---------------------------------------------------------------------------
Step 5 $total "Local AI models (~12 GB)"
# ---------------------------------------------------------------------------
$ollama = ""
foreach ($candidate in @(
    "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
    "$env:ProgramFiles\Ollama\ollama.exe"
)) {
    if (Test-Path $candidate) { $ollama = $candidate; break }
}
if (-not $ollama) {
    Say "  installing Ollama ..."
    $exe = Join-Path $env:TEMP "OllamaSetup.exe"
    try {
        Get-Downloaded "https://ollama.com/download/OllamaSetup.exe" $exe "Ollama"
        Start-Process -FilePath $exe -ArgumentList "/VERYSILENT","/NORESTART" -Wait
    } catch {
        Warn "Could not install Ollama automatically: $($_.Exception.Message)"
    }
    foreach ($candidate in @(
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
        "$env:ProgramFiles\Ollama\ollama.exe"
    )) {
        if (Test-Path $candidate) { $ollama = $candidate; break }
    }
}

if ($ollama) {
    Good "Ollama: $ollama"
    # Give the background service a moment; a pull against a server that has
    # not finished starting fails in a way that looks like a network problem.
    Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 5

    $hostModel  = Get-ConfigValue "hosts" "model" "qwen2.5:14b-instruct-q4_K_M"
    $chartModel = Get-ConfigValue "chart" "model" "qwen2.5vl:3b"

    # One at a time on purpose. Two concurrent pulls share the same bandwidth,
    # take the same total time, and each look stalled while they do it.
    foreach ($model in @($hostModel, $chartModel)) {
        $have = (& $ollama list 2>$null | Select-String -SimpleMatch $model)
        if ($have -and -not $Force) {
            Good "$model already present"
        } else {
            Say "  pulling $model  (this is the long one)"
            & $ollama pull $model
            if ($LASTEXITCODE -ne 0) { Warn "could not pull $model -- the hosts will stay quiet until it is there" }
        }
    }
} else {
    Warn "Ollama is not installed. The two AI hosts will not talk, but the"
    Warn "stream still runs on its template library. Install from ollama.com"
    Warn "and run this setup again to switch them on."
}

# ---------------------------------------------------------------------------
Step 6 $total "MetaTrader 5 (live prices)"
# ---------------------------------------------------------------------------
if (Test-Path "$env:ProgramFiles\MetaTrader 5\terminal64.exe") {
    Good "MetaTrader 5 already installed"
} else {
    Say "  installing MetaTrader 5 ..."
    try {
        $exe = Join-Path $env:TEMP "mt5setup.exe"
        Get-Downloaded "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" $exe "MetaTrader 5"
        Start-Process -FilePath $exe -ArgumentList "/auto" -Wait
        Good "MetaTrader 5 installed"
    } catch {
        Warn "Could not install MetaTrader 5: $($_.Exception.Message)"
        Warn "Install it yourself and log into any account (a free demo is enough)."
    }
}

# ---------------------------------------------------------------------------
Set-Content -Path $Stamp -Value $SetupVersion -Encoding utf8

Write-Host ""
Write-Host "  Setup complete." -ForegroundColor Green
Write-Host ""
Say "  Before the first run:" DarkGray
Say "    1. Open MetaTrader 5 and log in (a free demo account is fine)." DarkGray
Say "    2. Leave it running -- the narrator reads prices from it." DarkGray
Say "    3. Start 'Gamechanger Talker' from the Start Menu or desktop." DarkGray
Write-Host ""

if (-not $NoPause) { Read-Host "Press Enter to close" }
