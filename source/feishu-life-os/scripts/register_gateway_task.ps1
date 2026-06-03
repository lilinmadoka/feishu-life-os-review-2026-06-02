. "$PSScriptRoot\_gateway_common.ps1"

$root = Get-ProjectRoot
$taskName = "FeishuLifeOSGateway"
$scriptPath = Join-Path $root "scripts\start_local_gateway.ps1"

if (-not (Test-Path $scriptPath)) {
    throw "Start script not found: $scriptPath"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel LeastPrivilege

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

$task = New-ScheduledTask -Action $action -Principal $principal -Settings $settings
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null

Write-Host "Registered scheduled task: $taskName"
Write-Host "It has no trigger by default. Start it manually from Task Scheduler or run:"
Write-Host "  Start-ScheduledTask -TaskName $taskName"
