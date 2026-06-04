$ErrorActionPreference = "Stop"

function Get-ProjectRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Initialize-GatewayDirs {
    param([string]$Root)
    $data = Join-Path $Root ".data"
    $logs = Join-Path $data "logs"
    $pids = Join-Path $data "pids"
    New-Item -ItemType Directory -Force -Path $logs, $pids | Out-Null
    return @{
        Data = $data
        Logs = $logs
        Pids = $pids
    }
}

function Import-DotEnv {
    param([string]$Root)
    $envPath = Join-Path $Root ".env"
    if (-not (Test-Path $envPath)) {
        return
    }
    Get-Content -LiteralPath $envPath -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }
        $name, $value = $line.Split("=", 2)
        $name = $name.Trim()
        $value = $value.Trim().Trim('"').Trim("'")
        if ($name) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function Get-PidPath {
    param(
        [string]$Root,
        [string]$Name
    )
    return (Join-Path (Join-Path (Join-Path $Root ".data") "pids") "$Name.pid")
}

function Get-LogPath {
    param(
        [string]$Root,
        [string]$Name,
        [string]$Stream
    )
    return (Join-Path (Join-Path (Join-Path $Root ".data") "logs") "$Name.$Stream.log")
}

function Get-TrackedProcess {
    param(
        [string]$Root,
        [string]$Name
    )
    $pidPath = Get-PidPath -Root $Root -Name $Name
    if (-not (Test-Path $pidPath)) {
        return $null
    }
    $rawPid = (Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $rawPid) {
        return $null
    }
    return Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue
}

function Save-ProcessId {
    param(
        [string]$Root,
        [string]$Name,
        [int]$ProcessId
    )
    $pidPath = Get-PidPath -Root $Root -Name $Name
    Set-Content -LiteralPath $pidPath -Value $ProcessId -Encoding ASCII
}

function Stop-TrackedProcess {
    param(
        [string]$Root,
        [string]$Name
    )
    $process = Get-TrackedProcess -Root $Root -Name $Name
    if ($process) {
        Stop-Process -Id $process.Id -Force
        Write-Host "Stopped $Name (PID $($process.Id))"
    } else {
        Write-Host "$Name is not running"
    }
    $pidPath = Get-PidPath -Root $Root -Name $Name
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

function Get-PortProcessId {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($conn) {
        return [int]$conn.OwningProcess
    }
    return $null
}

function Get-CloudflaredPath {
    $command = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $candidates = @(
        "C:\Program Files\cloudflared\cloudflared.exe",
        "C:\Program Files (x86)\cloudflared\cloudflared.exe",
        (Join-Path $env:LOCALAPPDATA "cloudflared\cloudflared.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }
    return $null
}

function Get-RuntimeAdminToken {
    param([string]$Root)
    if ($env:ADMIN_API_TOKEN) {
        return $env:ADMIN_API_TOKEN
    }
    $runtimePath = Join-Path (Join-Path $Root ".data") "runtime_admin_token.txt"
    if (Test-Path $runtimePath) {
        $existing = (Get-Content -LiteralPath $runtimePath -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($existing) {
            $env:ADMIN_API_TOKEN = $existing.Trim()
            return $env:ADMIN_API_TOKEN
        }
    }
    $token = "local-" + [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N").Substring(0, 12)
    Set-Content -LiteralPath $runtimePath -Value $token -Encoding ASCII
    $env:ADMIN_API_TOKEN = $token
    return $token
}

function Get-LmsPath {
    $command = Get-Command lms -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $candidate = Join-Path $env:USERPROFILE ".lmstudio\bin\lms.exe"
    if (Test-Path $candidate) {
        return $candidate
    }
    return $null
}

function Get-LmStudioExePath {
    $programFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\LM Studio\LM Studio.exe"),
        (Join-Path $env:LOCALAPPDATA "LM Studio\LM Studio.exe"),
        (Join-Path $env:PROGRAMFILES "LM Studio\LM Studio.exe")
    )
    if ($programFilesX86) {
        $candidates += (Join-Path $programFilesX86 "LM Studio\LM Studio.exe")
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }
    return $null
}

function Get-LmStudioBaseUrl {
    if ($env:LM_STUDIO_BASE_URL) {
        return $env:LM_STUDIO_BASE_URL.TrimEnd("/")
    }
    return "http://127.0.0.1:1234/v1"
}

function Test-LmStudioServer {
    param([string]$BaseUrl)
    try {
        $models = Invoke-RestMethod -Uri "$($BaseUrl.TrimEnd('/'))/models" -TimeoutSec 3
        $count = 0
        if ($models.data) {
            $count = @($models.data).Count
        }
        return @{
            Ok = $true
            ModelCount = $count
            Error = $null
        }
    } catch {
        return @{
            Ok = $false
            ModelCount = 0
            Error = $_.Exception.Message
        }
    }
}

function Stop-ProjectFastApiListener {
    param([string]$Root)
    $portPid = Get-PortProcessId -Port 8000
    if (-not $portPid) {
        return
    }
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$portPid"
        if ($proc -and $proc.CommandLine -like "*$Root*" -and $proc.CommandLine -like "*uvicorn*app.main:app*") {
            Stop-Process -Id $portPid -Force
            Remove-Item -LiteralPath (Get-PidPath -Root $Root -Name "fastapi") -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped existing project FastAPI listener (PID $portPid)"
            Start-Sleep -Seconds 1
        }
    } catch {
        Write-Warning "Could not inspect/stop existing port 8000 listener: $($_.Exception.Message)"
    }
}
