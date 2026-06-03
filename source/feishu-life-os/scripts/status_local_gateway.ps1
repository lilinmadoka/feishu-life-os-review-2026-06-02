. "$PSScriptRoot\_gateway_common.ps1"

$root = Get-ProjectRoot
Initialize-GatewayDirs -Root $root | Out-Null

function Show-TrackedStatus {
    param([string]$Name)
    $process = Get-TrackedProcess -Root $root -Name $Name
    if ($process) {
        Write-Host "${Name}: running (PID $($process.Id))"
    } else {
        Write-Host "${Name}: stopped"
    }
}

Show-TrackedStatus -Name "fastapi"
Show-TrackedStatus -Name "cloudflared"
Show-TrackedStatus -Name "codex_worker"
Show-TrackedStatus -Name "reminder_worker"

$portPid = Get-PortProcessId -Port 8000
if ($portPid) {
    Write-Host "Port 8000: listening (PID $portPid)"
} else {
    Write-Host "Port 8000: not listening"
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 5
    Write-Host "Health: $($health | ConvertTo-Json -Compress)"
} catch {
    Write-Host "Health: unavailable ($($_.Exception.Message))"
}

$cloudflaredOut = Get-LogPath -Root $root -Name "cloudflared" -Stream "out"
$cloudflaredErr = Get-LogPath -Root $root -Name "cloudflared" -Stream "err"
$combined = ""
if (Test-Path $cloudflaredOut) { $combined += "`n" + (Get-Content -LiteralPath $cloudflaredOut -Raw -ErrorAction SilentlyContinue) }
if (Test-Path $cloudflaredErr) { $combined += "`n" + (Get-Content -LiteralPath $cloudflaredErr -Raw -ErrorAction SilentlyContinue) }
$match = [regex]::Match($combined, "https://[a-zA-Z0-9-]+\.trycloudflare\.com")
if ($match.Success) {
    Write-Host "Feishu callback URL (Agent-first v2): $($match.Value)/api/v2/feishu/events"
    Write-Host "Legacy callback URL: $($match.Value)/api/feishu/events"
} else {
    Write-Host "Feishu callback URL: not detected"
}

Write-Host "Logs directory: $(Join-Path (Join-Path $root ".data") "logs")"
