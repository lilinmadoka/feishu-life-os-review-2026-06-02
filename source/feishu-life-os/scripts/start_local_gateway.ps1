param(
    [switch]$NoWorker
)

. "$PSScriptRoot\_gateway_common.ps1"

$root = Get-ProjectRoot
$dirs = Initialize-GatewayDirs -Root $root
Import-DotEnv -Root $root
if ($env:LIFEOS_START_FULL_ENABLE_OBSERVABILITY -eq "true") {
    $env:OBSERVABILITY_ENABLED = "true"
    $env:OBSERVABILITY_CAPTURE_FULL_PAYLOAD = "false"
}

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python virtual environment not found: $python"
}

$portPid = Get-PortProcessId -Port 8000
if ($portPid) {
    Write-Host "FastAPI is already listening on 127.0.0.1:8000 (PID $portPid)"
    Save-ProcessId -Root $root -Name "fastapi" -ProcessId $portPid
} else {
    $stdout = Get-LogPath -Root $root -Name "fastapi" -Stream "out"
    $stderr = Get-LogPath -Root $root -Name "fastapi" -Stream "err"
    $process = Start-Process -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $root `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru
    Save-ProcessId -Root $root -Name "fastapi" -ProcessId $process.Id
    Write-Host "Started FastAPI (PID $($process.Id))"
    Start-Sleep -Seconds 2
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 5
    Write-Host "Health: $($health | ConvertTo-Json -Compress)"
} catch {
    Write-Warning "FastAPI health check failed: $($_.Exception.Message)"
}

$cloudflaredPath = Get-CloudflaredPath
if (-not $cloudflaredPath) {
    throw "cloudflared is not installed. Install it first, then rerun this script. See docs/LOCAL_GATEWAY.md."
}

$tunnelMode = if ($env:TUNNEL_MODE) { $env:TUNNEL_MODE } else { "quick" }
$existingTunnel = Get-TrackedProcess -Root $root -Name "cloudflared"
$callbackUrl = $null

if ($existingTunnel) {
    Write-Host "Cloudflare Tunnel is already running (PID $($existingTunnel.Id))"
} else {
    $stdout = Get-LogPath -Root $root -Name "cloudflared" -Stream "out"
    $stderr = Get-LogPath -Root $root -Name "cloudflared" -Stream "err"
    if ($tunnelMode -eq "quick") {
        $arguments = @("tunnel", "--url", "http://127.0.0.1:8000")
    } elseif ($tunnelMode -eq "named") {
        if (-not $env:CLOUDFLARE_TUNNEL_HOSTNAME) {
            throw "CLOUDFLARE_TUNNEL_HOSTNAME is required when TUNNEL_MODE=named"
        }
        throw "TUNNEL_MODE=named is reserved for a future fixed-domain setup. Use TUNNEL_MODE=quick until a Cloudflare domain is configured."
    } else {
        throw "Unsupported TUNNEL_MODE: $tunnelMode"
    }

    $process = Start-Process -FilePath $cloudflaredPath `
        -ArgumentList $arguments `
        -WorkingDirectory $root `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru
    Save-ProcessId -Root $root -Name "cloudflared" -ProcessId $process.Id
    Write-Host "Started Cloudflare Tunnel (PID $($process.Id))"
}

if ($tunnelMode -eq "quick") {
    $stdout = Get-LogPath -Root $root -Name "cloudflared" -Stream "out"
    $stderr = Get-LogPath -Root $root -Name "cloudflared" -Stream "err"
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        $combined = ""
        if (Test-Path $stdout) { $combined += "`n" + (Get-Content -LiteralPath $stdout -Raw -ErrorAction SilentlyContinue) }
        if (Test-Path $stderr) { $combined += "`n" + (Get-Content -LiteralPath $stderr -Raw -ErrorAction SilentlyContinue) }
        $match = [regex]::Match($combined, "https://[a-zA-Z0-9-]+\.trycloudflare\.com")
        if ($match.Success) {
            $callbackUrl = "$($match.Value)/api/v2/feishu/events"
            break
        }
    }
}

if (-not $NoWorker) {
    & "$PSScriptRoot\start_codex_worker.ps1"
    & "$PSScriptRoot\start_reminder_worker.ps1"
} else {
    Write-Host "Skipped worker startup because -NoWorker was supplied"
}

if ($callbackUrl) {
    Write-Host ""
    Write-Host "Feishu callback URL:"
    Write-Host "  $callbackUrl"
    Write-Host ""
    Write-Host "Copy this Agent-first v2 URL into Feishu Open Platform -> Events and Callbacks -> Developer server."
    Write-Host "Legacy callback remains available at: $($callbackUrl -replace '/api/v2/feishu/events$', '/api/feishu/events')"
} else {
    Write-Warning "Could not detect a trycloudflare URL yet. Check logs:"
    Write-Host "  $(Get-LogPath -Root $root -Name "cloudflared" -Stream "out")"
    Write-Host "  $(Get-LogPath -Root $root -Name "cloudflared" -Stream "err")"
}
