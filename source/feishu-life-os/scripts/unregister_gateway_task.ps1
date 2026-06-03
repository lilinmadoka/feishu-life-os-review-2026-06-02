$taskName = "FeishuLifeOSGateway"

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Unregistered scheduled task: $taskName"
} else {
    Write-Host "Scheduled task not found: $taskName"
}
