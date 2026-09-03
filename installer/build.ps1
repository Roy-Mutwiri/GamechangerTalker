# Build GamechangerTalkerSetup.exe.
#
#   powershell -ExecutionPolicy Bypass -File installer\build.ps1
#
# Needs Inno Setup 6:  winget install --id JRSoftware.InnoSetup
# Output lands in dist\GamechangerTalkerSetup.exe

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $PSScriptRoot "GamechangerTalker.iss"
$Dist = Join-Path $Root "dist"

# winget puts Inno Setup under LocalAppData for a per-user install and under
# Program Files for a machine-wide one, and neither is on PATH.
$candidates = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$iscc = ""
foreach ($c in $candidates) { if (Test-Path $c) { $iscc = $c; break } }
if (-not $iscc) {
    $found = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($found) { $iscc = $found.Source }
}
if (-not $iscc) {
    Write-Host "Inno Setup not found. Install it with:" -ForegroundColor Red
    Write-Host "  winget install --id JRSoftware.InnoSetup" -ForegroundColor Yellow
    exit 1
}

Write-Host "compiler : $iscc" -ForegroundColor DarkGray
Write-Host "script   : $Script" -ForegroundColor DarkGray

if (-not (Test-Path $Dist)) { New-Item -ItemType Directory -Path $Dist | Out-Null }

& $iscc $Script
if ($LASTEXITCODE -ne 0) {
    Write-Host "build failed" -ForegroundColor Red
    exit $LASTEXITCODE
}

$exe = Join-Path $Dist "GamechangerTalkerSetup.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "built: $exe  ($mb MB)" -ForegroundColor Green
} else {
    Write-Host "compiler reported success but no exe was produced" -ForegroundColor Red
    exit 1
}
