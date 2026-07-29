# Launch the narrator in this window, using the project's venv.
#
#   .\run.ps1                     live: MT5 + Kokoro + Warudo
#   .\run.ps1 -Replay             recorded bars, real audio, real time
#   .\run.ps1 -Replay -DryRun     transcript only, no audio, fast
#
# Anything you type while it runs is spoken next at priority 5.
# Commands: /mute /unmute /skip /reload /quiet 300 /quit

param(
    [switch]$Replay,
    [switch]$DryRun,
    [switch]$NoAvatar,
    [switch]$Plain,
    [double]$Speed = 0,
    [double]$Minutes = 0,
    [string]$Voice = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "No venv found. Create it first:" -ForegroundColor Yellow
    Write-Host "  py -3.11 -m venv .venv"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

$narratorArgs = @("-m", "narrator.main")
if ($Replay)   { $narratorArgs += "--replay" }
if ($DryRun)   { $narratorArgs += "--dry-run" }
if ($NoAvatar) { $narratorArgs += "--no-avatar" }
if ($Plain)    { $narratorArgs += "--plain" }
if ($Speed -gt 0)   { $narratorArgs += @("--speed", $Speed) }
if ($Minutes -gt 0) { $narratorArgs += @("--minutes", $Minutes) }
if ($Voice -ne "")  { $narratorArgs += @("--voice", $Voice) }

# The dashboard wants room. 120x40 is the comfortable minimum.
try {
    $host.UI.RawUI.WindowTitle = "trade fix narrator"
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
    # Terminal does not allow resizing (Windows Terminal). Not fatal.
}

& $python @narratorArgs
