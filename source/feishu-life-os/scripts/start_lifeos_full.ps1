param(
    [switch]$NoOpenBrowser
)

. "$PSScriptRoot\_gateway_common.ps1"

$root = Get-ProjectRoot
Initialize-GatewayDirs -Root $root | Out-Null
Import-DotEnv -Root $root

$env:OBSERVABILITY_ENABLED = "true"
$env:OBSERVABILITY_CAPTURE_FULL_PAYLOAD = "false"
$env:LIFEOS_START_FULL_ENABLE_OBSERVABILITY = "true"
$adminToken = Get-RuntimeAdminToken -Root $root

Write-Host "LifeOS full startup"
Write-Host "Project: $root"
Write-Host "Observability: enabled, full payload capture disabled"

$lmBaseUrl = Get-LmStudioBaseUrl
$lms = Get-LmsPath
$lmExe = Get-LmStudioExePath

$lmStudioProcess = Get-Process -Name "LM Studio" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($lmExe -and -not $lmStudioProcess) {
    try {
        Start-Process -FilePath $lmExe -WindowStyle Hidden | Out-Null
        Write-Host "Started LM Studio app"
        Start-Sleep -Seconds 2
    } catch {
        Write-Warning "Could not start LM Studio app: $($_.Exception.Message)"
    }
}

if ($lms) {
    try {
        & $lms server start | Write-Host
    } catch {
        Write-Warning "lms server start failed: $($_.Exception.Message)"
    }

    if ($env:LM_STUDIO_MODEL) {
        $loadArgs = @("load", $env:LM_STUDIO_MODEL, "-y")
        $contextLength = 0
        if ([int]::TryParse([string]$env:LM_STUDIO_CONTEXT_LENGTH, [ref]$contextLength) -and $contextLength -gt 0) {
            $loadArgs += @("-c", [string]$contextLength)
        }
        try {
            Write-Host "Loading LM Studio model: $env:LM_STUDIO_MODEL"
            & $lms @loadArgs | Write-Host
        } catch {
            Write-Warning "lms load failed. You may need to choose/load the model in LM Studio: $($_.Exception.Message)"
        }
    } else {
        Write-Warning "LM_STUDIO_MODEL is not set. LM Studio server will start, but no model is loaded by this script."
    }
} else {
    Write-Warning "lms.exe was not found. Install/enable LM Studio CLI, or start the Local Server manually."
}

$lmReady = $false
for ($i = 0; $i -lt 45; $i++) {
    $status = Test-LmStudioServer -BaseUrl $lmBaseUrl
    if ($status.Ok) {
        Write-Host "LM Studio server reachable at $lmBaseUrl/models (models: $($status.ModelCount))"
        $lmReady = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $lmReady) {
    Write-Warning "LM Studio server is still unavailable at $lmBaseUrl/models. LifeOS will start, but model-stage traces will fail until LM Studio is running."
}

Stop-ProjectFastApiListener -Root $root

& "$PSScriptRoot\start_local_gateway.ps1"

$uiUrl = "http://127.0.0.1:8000/api/v2/observability/ui?admin_token=$adminToken"
Write-Host ""
Write-Host "Observability UI:"
Write-Host "  $uiUrl"

if (-not $NoOpenBrowser) {
    Start-Process $uiUrl | Out-Null
}
