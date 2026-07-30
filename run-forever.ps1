# Keep the narrator running.
#
#   .\run-forever.ps1                 live MT5, browser UI, chart eyes
#   .\run-forever.ps1 -Mute           same, but silent until /unmute
#   .\run-forever.ps1 -Once           run once and stop (no restarts)
#
# A stream is a thing that should be up, not a thing somebody remembers to
# start. This restarts the narrator whenever it exits, for any reason.
#
# WHY THE BACKOFF MATTERS. The narrator refuses to start when its prices are
# not real time -- no MetaTrader, a dead feed -- and that refusal is correct,
# so it exits immediately and would be restarted immediately. Left alone that
# is a loop hammering a broker terminal several times a second and writing a
# gigabyte of identical log lines overnight.
#
# So failures back off: 5s, 10s, 20s, up to a minute between attempts. A run
# that lasted longer than SETTLED_SECONDS is treated as healthy and resets the
# delay, which distinguishes "crashed after four hours" (restart now) from
# "cannot start at all" (wait, and stop filling the disk).
#
# Ctrl+C stops the supervisor and the narrator with it.

param(
    [switch]$Mute,
    [switch]$Once,
    [switch]$NoWeb
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "No venv found. See run.ps1 for setup." -ForegroundColor Yellow
    exit 1
}

$log = Join-Path $PSScriptRoot "logs\supervisor.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$narratorArgs = @("-m", "narrator.main", "--plain")
if ($Mute)   { $narratorArgs += "--mute" }
if ($NoWeb)  { $narratorArgs += "--no-web" }

# A run shorter than this is a failure to start, not a crash after working.
$SETTLED_SECONDS = 120
$MIN_DELAY = 5
$MAX_DELAY = 60
$delay = $MIN_DELAY
$attempt = 0

function Write-Log([string]$message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

Write-Log "supervisor started (pid $PID)"
$env:PYTHONPATH = $PSScriptRoot

try {
    while ($true) {
        $attempt++
        Write-Log "starting narrator (attempt $attempt)"
        $started = Get-Date

        & $python @narratorArgs
        $code = $LASTEXITCODE

        $ran = [int]((Get-Date) - $started).TotalSeconds
        Write-Log "narrator exited with code $code after ${ran}s"

        if ($Once) {
            Write-Log "-Once was set; not restarting"
            break
        }

        if ($ran -ge $SETTLED_SECONDS) {
            # It was up and doing its job, so whatever ended it was not a
            # configuration problem. Come straight back.
            $delay = $MIN_DELAY
        } else {
            Write-Log "exited quickly -- check the preflight output above"
        }

        Write-Log "restarting in ${delay}s"
        Start-Sleep -Seconds $delay
        if ($ran -lt $SETTLED_SECONDS) {
            $delay = [Math]::Min($delay * 2, $MAX_DELAY)
        }
    }
}
finally {
    Write-Log "supervisor stopping (pid $PID)"
}
