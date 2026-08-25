# Install the packaged app for the current Windows user.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dist = Join-Path $Root "dist"
$Source = Get-ChildItem -Path $Dist -Directory | Select-Object -First 1
if (-not $Source) {
    Write-Host "Cannot find dist folder. Run pack.bat first."
    exit 1
}
$Source = $Source.FullName
$InstallDir = Join-Path $env:LOCALAPPDATA "sheets-post-filter"
$Exe = Get-ChildItem -Path $Source -Filter "*.exe" | Select-Object -First 1
if (-not $Exe) {
    Write-Host "Cannot find packaged exe in $Source"
    exit 1
}

Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir) } | ForEach-Object {
    Write-Host "Stopping running app..."
    $_ | Stop-Process -Force
    Start-Sleep -Seconds 1
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Write-Host "Copying to $InstallDir"

$existConfig = Join-Path $InstallDir "config.json"
$backupConfig = $null
if (Test-Path $existConfig) {
    $backupConfig = Get-Content $existConfig -Raw -Encoding UTF8
}

& robocopy $Source $InstallDir /E /NFL /NDL /NJH /NJS | Out-Null
if ($LastExitCode -ge 8) {
    Write-Host "Copy failed, robocopy exit $LastExitCode"
    exit 1
}

if ($backupConfig) {
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($existConfig, $backupConfig.TrimStart([char]0xFEFF), $utf8)
    Write-Host "Kept existing config.json"
}

Copy-Item (Join-Path $Root "uninstall-template.bat") (Join-Path $InstallDir "uninstall.bat") -Force

$exePath = Join-Path $InstallDir $Exe.Name
# Explorer shortcuts often fail to read icons from a Chinese-named exe.
# Keep an ASCII .ico next to the exe and point the shortcut at it.
$iconPath = Join-Path $InstallDir "logo.ico"
$iconCandidates = @(
    (Join-Path $Root "logo.ico"),
    (Join-Path $Source "logo.ico"),
    (Join-Path $Source "_internal\logo.ico"),
    (Join-Path $InstallDir "_internal\logo.ico")
)
foreach ($c in $iconCandidates) {
    if (Test-Path $c) {
        Copy-Item $c $iconPath -Force
        break
    }
}
$Wsh = New-Object -ComObject WScript.Shell

function New-AppShortcut([string]$path) {
    $sc = $Wsh.CreateShortcut($path)
    $sc.TargetPath = $exePath
    $sc.WorkingDirectory = $InstallDir
    $sc.WindowStyle = 1
    if (Test-Path $iconPath) {
        $sc.IconLocation = "$iconPath,0"
    } else {
        $sc.IconLocation = "$exePath,0"
    }
    $sc.Save()
}

$cn = -join [char[]](0x6570, 0x636E, 0x6C47, 0x603B, 0x5DE5, 0x5177)
$desk = Join-Path ([Environment]::GetFolderPath("Desktop")) ($cn + ".lnk")
$start = Join-Path $env:APPDATA ("Microsoft\Windows\Start Menu\Programs\" + $cn + ".lnk")
New-AppShortcut $desk
New-AppShortcut $start

Write-Host "Install done."
Write-Host "App folder: $InstallDir"
Write-Host "Desktop shortcut created."
Write-Host "Uninstall file: uninstall.bat in the app folder."
exit 0
