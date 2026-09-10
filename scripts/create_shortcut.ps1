# Put a Leasure shortcut on the Windows desktop (and optionally the Start menu).
# Usage: scripts\create_shortcut.ps1 [-Name Leasure] [-StartMenu]
# Safe to re-run; an existing shortcut of the same name is overwritten.
#
# If PowerShell refuses to run this script, allow scripts for this session first:
#   Set-ExecutionPolicy -Scope Process Bypass

param([string]$Name = "Leasure", [switch]$StartMenu)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $RepoRoot "scripts\launch.ps1"
$IconPath = Join-Path $RepoRoot "static\img\leasure.ico"

if (-not (Test-Path $Launcher)) { Write-Error "Launcher not found at $Launcher" }
if (-not (Test-Path $IconPath)) { Write-Error "Icon not found at $IconPath. Run: python scripts\make_icon.py" }

$Shell = New-Object -ComObject WScript.Shell

# GetFolderPath resolves a OneDrive-redirected Desktop, which $env:USERPROFILE does not.
$targets = @([Environment]::GetFolderPath('Desktop'))
if ($StartMenu) { $targets += [Environment]::GetFolderPath('Programs') }

foreach ($folder in $targets) {
    $linkPath = Join-Path $folder "$Name.lnk"
    $link = $Shell.CreateShortcut($linkPath)
    $link.TargetPath = "powershell.exe"
    $link.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`""
    $link.WorkingDirectory = $RepoRoot
    $link.IconLocation = "$IconPath,0"
    $link.Description = "Start Leasure and open it in your browser"
    $link.Save()
    Write-Host "Created $linkPath"
}
