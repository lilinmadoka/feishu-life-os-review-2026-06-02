. "$PSScriptRoot\_gateway_common.ps1"

$root = Get-ProjectRoot
Initialize-GatewayDirs -Root $root | Out-Null
Stop-TrackedProcess -Root $root -Name "codex_worker"
