. "$PSScriptRoot\_gateway_common.ps1"

$root = Get-ProjectRoot
Initialize-GatewayDirs -Root $root | Out-Null
Import-DotEnv -Root $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python virtual environment not found: $python"
}

$existing = Get-TrackedProcess -Root $root -Name "reminder_worker"
if ($existing) {
    Write-Host "Reminder worker is already running (PID $($existing.Id))"
    exit 0
}

$stdout = Get-LogPath -Root $root -Name "reminder_worker" -Stream "out"
$stderr = Get-LogPath -Root $root -Name "reminder_worker" -Stream "err"
$process = Start-Process -FilePath $python `
    -ArgumentList @("-m", "app.workers.reminder_worker") `
    -WorkingDirectory $root `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru
Save-ProcessId -Root $root -Name "reminder_worker" -ProcessId $process.Id
Write-Host "Started reminder worker (PID $($process.Id))"
