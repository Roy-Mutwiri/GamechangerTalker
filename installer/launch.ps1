# Gamechanger Talker -- launcher.
#
# What the Start Menu and desktop shortcuts point at. Three jobs, in order:
# finish setup if it never finished, say something useful if MetaTrader is not
# up, and then get out of the way and run the narrator.
#
# The point of the MetaTrader check is that "no live prices" is by far the most
# common thing to go wrong on a fresh machine, and the narrator's own preflight
# reports it correctly but in the language of the codebase. Someone who just
# installed an exe deserves the sentence that tells them what to click.

param(
    [switch]$Replay,     # recorded bars instead of live prices
    [switch]$Plain,      # scrolling transcript instead of the dashboard
    [switch]$NoAvatar
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Stamp = Join-Path $Root ".setup-complete"

$host.UI.RawUI.WindowTitle = "Gamechanger Talker"

# --- finish setup if it did not ------------------------------------------
if (-not (Test-Path $Stamp) -or -not (Test-Path $Python)) {
    Write-Host ""
    Write-Host "  First run -- finishing setup." -ForegroundColor Cyan
    Write-Host ""
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "setup.ps1") -NoPause
    if (-not (Test-Path $Python)) {
        Write-Host ""
        Write-Host "  Setup did not complete. Run it again from the Start Menu:" -ForegroundColor Red
        Write-Host "  'Gamechanger Talker Setup'" -ForegroundColor Red
        Read-Host "Press Enter to close"
        exit 1
    }
}

# --- MetaTrader has to be running for live prices -------------------------
if (-not $Replay) {
    $mt5 = Get-Process terminal64 -ErrorAction SilentlyContinue
    if (-not $mt5) {
        $terminal = "$env:ProgramFiles\MetaTrader 5\terminal64.exe"
        if (Test-Path $terminal) {
            Write-Host ""
            Write-Host "  MetaTrader 5 is not running -- starting it." -ForegroundColor Yellow
            Write-Host "  Log in when it opens (a free demo account is fine), then leave it open." -ForegroundColor DarkGray
            Start-Process $terminal
            Write-Host ""
            Write-Host "  Waiting for MetaTrader to come up ..." -ForegroundColor DarkGray
            for ($i = 0; $i -lt 30; $i++) {
                Start-Sleep -Seconds 2
                if (Get-Process terminal64 -ErrorAction SilentlyContinue) { break }
            }
        } else {
            Write-Host ""
            Write-Host "  MetaTrader 5 is not installed, so there are no live prices." -ForegroundColor Yellow
            Write-Host "  Starting in replay mode instead (recorded bars, real speech)." -ForegroundColor Yellow
            Write-Host ""
            $Replay = $true
        }
    }
}

# --- run ------------------------------------------------------------------
$narratorArgs = @("-m", "narrator.main")
if ($Replay)   { $narratorArgs += @("--replay", "--allow-delayed") }
if ($Plain)    { $narratorArgs += "--plain" }
if ($NoAvatar) { $narratorArgs += "--no-avatar" }

# Warudo is optional and most machines will not have it. Connecting to a bridge
# that is not there costs a timeout at startup and prints a warning that reads
# like a fault, so it is off unless the avatar is actually installed.
if (-not $NoAvatar -and -not (Test-Path "$env:APPDATA\Warudo")) {
    $narratorArgs += "--no-avatar"
}

$env:PYTHONIOENCODING = "utf-8"

# The dashboard wants room. 120x40 is the comfortable minimum.
try {
    $size = $host.UI.RawUI.WindowSize
    if ($size.Width -lt 120 -or $size.Height -lt 36) {
        $buffer = $host.UI.RawUI.BufferSize
        $buffer.Width = [Math]::Max(120, $buffer.Width)
        $buffer.Height = [Math]::Max(3000, $buffer.Height)
        $host.UI.RawUI.BufferSize = $buffer
        $size.Width = 120
        $size.Height = 40
        $host.UI.RawUI.WindowSize = $size
    }
} catch {
    # Windows Terminal does not allow this. Not fatal.
}

Write-Host ""
Write-Host "  Starting. The browser view opens at http://127.0.0.1:8770" -ForegroundColor Cyan
Write-Host "  Type a line to have it spoken. /quit to stop." -ForegroundColor DarkGray
Write-Host ""

& $Python @narratorArgs

$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ""
    Write-Host "  The narrator stopped with an error (exit $code)." -ForegroundColor Red
    Write-Host "  The most common cause is MetaTrader 5 not being logged in." -ForegroundColor DarkGray
    Read-Host "Press Enter to close"
}
