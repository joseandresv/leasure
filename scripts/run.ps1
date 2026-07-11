# Start the Leasure server (native Windows, PowerShell).
# Usage: scripts\run.ps1 [-Port 8643]   (default 8642; pick another port if a
# WSL2 Leasure instance is running — WSL forwards its ports to Windows localhost)

param([int]$Port = 8642)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$VenvPython = ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Error ".venv not found. Run scripts\install.ps1 first."
}

& $VenvPython -m uvicorn app:app --host 127.0.0.1 --port $Port
