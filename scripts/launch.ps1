# Start Leasure (or reuse the instance already running) and open it in the browser.
# Usage: scripts\launch.ps1 [-Port 8642] [-NoBrowser] [-Stop]
#
# Uses .venv\Scripts\python.exe when the repo has a native Windows install,
# otherwise starts the server inside WSL2 (WSL forwards the port to Windows
# localhost, so the browser reaches it either way). Set LEASURE_WSL_DISTRO to
# pick a distro other than the default one.
#
# If PowerShell refuses to run this script, allow scripts for this session first:
#   Set-ExecutionPolicy -Scope Process Bypass

param([int]$Port = 8642, [switch]$NoBrowser, [switch]$Stop)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Url = "http://localhost:$Port/"
$PidFile = Join-Path $RepoRoot "data\leasure.pid"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LastProbeError = "no response"
$ManualHint = "scripts\run.ps1 -Port $Port"

$DistroArgs = @()
if ($env:LEASURE_WSL_DISTRO) { $DistroArgs = @("-d", $env:LEASURE_WSL_DISTRO) }

function Test-LeasureServer {
    try {
        # 5 s, not 2: the first Invoke-WebRequest in a fresh PowerShell process
        # spends ~2 s loading the web stack, which would look like "not running".
        Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 | Out-Null
        return $true
    } catch {
        $script:LastProbeError = $_.Exception.Message
        return $false
    }
}

function Get-NativeServerProcess {
    if (-not (Test-Path $PidFile)) { return $null }
    $recorded = (Get-Content $PidFile -TotalCount 1).Trim()
    if (-not $recorded) { return $null }
    return Get-Process -Id ([int]$recorded) -ErrorAction SilentlyContinue
}

function Invoke-Wsl {
    param([string]$Command)

    $output = & wsl.exe @DistroArgs -- bash -lc $Command
    # A missing or broken default distro is the common failure here; Ubuntu is
    # what the install docs use, so try it before giving up.
    if ($LASTEXITCODE -ne 0 -and $DistroArgs.Count -eq 0) {
        $script:DistroArgs = @("-d", "Ubuntu")
        $output = & wsl.exe @DistroArgs -- bash -lc $Command
    }
    return $output
}

if ($Stop) {
    $native = Get-NativeServerProcess
    if ($native) {
        Stop-Process -Id $native.Id -Force
        Write-Host "Stopped Leasure (pid $($native.Id))."
    } else {
        Invoke-Wsl "lsof -ti:$Port -sTCP:LISTEN | xargs -r kill" | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Error "Could not reach WSL to stop the server on port $Port." }
        Write-Host "Stopped Leasure on port $Port."
    }
    if (Test-Path $PidFile) { Remove-Item $PidFile }
    exit 0
}

if (Test-LeasureServer) {
    Write-Host "Leasure is already running on port $Port."
} elseif (Test-Path $VenvPython) {
    Write-Host "Starting Leasure (native venv) on port $Port ..."
    $proc = Start-Process -FilePath $VenvPython `
        -ArgumentList "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "$Port" `
        -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
    New-Item -ItemType Directory -Force -Path (Split-Path $PidFile) | Out-Null
    Set-Content -Path $PidFile -Value $proc.Id
} else {
    Write-Host "Starting Leasure (WSL2) on port $Port ..."
    # Forward slashes and single quotes: PowerShell drops the backslashes when it
    # builds wsl.exe's command line, and wslpath takes either separator.
    $posixRepo = Invoke-Wsl "wslpath -a '$($RepoRoot.Replace('\', '/'))'" | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0 -or -not $posixRepo) {
        Write-Error "No native .venv, and WSL could not translate $RepoRoot. Run scripts\install.ps1 for a Windows install."
    }
    $posixRepo = $posixRepo.Trim()
    $quotedRepo = "'" + $posixRepo.Replace("'", "'\''") + "'"
    $ManualHint = "wsl -- bash -lc `"cd $posixRepo && scripts/run.sh $Port`""
    # A venv under ~ instead of the repo is the documented layout when the checkout
    # sits on a Windows drive (drvfs), so prefer it when it is there.
    $start = "cd $quotedRepo || exit 1; " +
             "if [ -x ~/.venvs/leasure/bin/python ]; then export LEASURE_VENV=~/.venvs/leasure; fi; " +
             "exec scripts/run.sh $Port"
    # WSL kills every process of a wsl.exe session when that session ends, so
    # backgrounding inside WSL is not enough: a hidden wsl.exe has to own the server.
    $wslCommandLine = (($DistroArgs -join " ") + " -- bash -lc `"$start`"").Trim()
    Start-Process -FilePath "wsl.exe" -ArgumentList $wslCommandLine -WindowStyle Hidden | Out-Null
}

$deadline = 30
$waited = [Diagnostics.Stopwatch]::StartNew()
while ($waited.Elapsed.TotalSeconds -lt $deadline) {
    if (Test-LeasureServer) {
        if (-not $NoBrowser) { Start-Process $Url }
        Write-Host "Leasure is at $Url"
        exit 0
    }
    Start-Sleep -Seconds 1
}

Write-Host "Leasure did not answer at $Url within $deadline seconds. Last error: $LastProbeError"
Write-Host "Start it by hand to see why:"
Write-Host "  $ManualHint"
exit 1
