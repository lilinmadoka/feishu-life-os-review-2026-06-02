. "$PSScriptRoot\_gateway_common.ps1"

$root = Get-ProjectRoot
Initialize-GatewayDirs -Root $root | Out-Null

Stop-TrackedProcess -Root $root -Name "codex_worker"
Stop-TrackedProcess -Root $root -Name "reminder_worker"
Stop-TrackedProcess -Root $root -Name "cloudflared"
Stop-TrackedProcess -Root $root -Name "fastapi"

$portPid = Get-PortProcessId -Port 8000
if ($portPid) {
    Write-Warning "Port 8000 is still in use by PID $portPid. Stop it manually if this is the old FastAPI service."
} else {
    Write-Host "Port 8000 is free"
}
