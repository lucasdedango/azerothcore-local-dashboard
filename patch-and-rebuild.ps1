$ErrorActionPreference = "Stop"
Set-Location "C:\azerothcore-playerbots"

Write-Host "=== Patch selfbot ===" -ForegroundColor Cyan
& ".\modules\mod-server-customization\apply-playerbots-selfbot-lock.ps1"
if ($LASTEXITCODE -ne 0) {
    throw "apply-playerbots-selfbot-lock.ps1 failed with exit code $LASTEXITCODE"
}

Write-Host "`n=== Patch gather / grindtarget ===" -ForegroundColor Cyan
& ".\modules\mod-server-customization\apply-playerbots-gather-only.ps1"
if ($LASTEXITCODE -ne 0) {
    throw "apply-playerbots-gather-only.ps1 failed with exit code $LASTEXITCODE"
}

Write-Host "`n=== Rebuild AzerothCore ===" -ForegroundColor Cyan
& ".\rebuild-azerothcore.ps1"
