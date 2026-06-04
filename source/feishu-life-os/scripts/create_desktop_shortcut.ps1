. "$PSScriptRoot\_gateway_common.ps1"

function Decode-Utf8Base64 {
    param([string]$Value)
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}

$root = Get-ProjectRoot
$script = Join-Path $root "scripts\start_lifeos_full.ps1"
if (-not (Test-Path $script)) {
    throw "Startup script not found: $script"
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutName = Decode-Utf8Base64 -Value "5ZCv5Yqo6aOe5Lmm55Sf5rS75pON5L2c57O757ufLmxuaw=="
$description = Decode-Utf8Base64 -Value "5ZCv5YqoIExpZmVPUyDmnKzlnLDnvZHlhbPjgIFMTSBTdHVkaW/jgIFXb3JrZXLvvIzlubbmiZPlvIDop4LmtYvpobXpnaI="
$shortcutPath = Join-Path $desktop $shortcutName
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershell
$shortcut.Arguments = "-NoExit -NoProfile -ExecutionPolicy Bypass -File `"$script`""
$shortcut.WorkingDirectory = $root
$shortcut.Description = $description
$shortcut.WindowStyle = 1
$shortcut.Save()

Write-Host "Desktop shortcut created:"
Write-Host "  $shortcutPath"
