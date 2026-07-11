# Start the Leasure server (native Windows, PowerShell).

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$VenvPython = ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Error ".venv not found. Run scripts\install.ps1 first."
}

& $VenvPython -m uvicorn app:app --host 127.0.0.1 --port 8642
