. "$PSScriptRoot\_gateway_common.ps1"

$root = Get-ProjectRoot
Initialize-GatewayDirs -Root $root | Out-Null
Import-DotEnv -Root $root

Write-Host "Agent-first mode uses Codex CLI on demand inside FastAPI."
Write-Host "No background Codex review worker is started."
Write-Host "To reduce game/desktop load, stop the local gateway; there is no separate agent process to stop."
