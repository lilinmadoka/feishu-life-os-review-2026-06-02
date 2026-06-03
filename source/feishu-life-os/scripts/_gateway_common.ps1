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
