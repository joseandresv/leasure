# Leasure install script (native Windows, PowerShell).
# Safe to re-run.
#
# If PowerShell refuses to run this script, you may need to allow scripts
# for this session first:
#   Set-ExecutionPolicy -Scope Process Bypass

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# --- Find Python >= 3.11 ---
function Get-PythonCommand {
    foreach ($candidate in @(@("py", "-3"), @("python"))) {
        $exe = $candidate[0]
        $extraArgs = $candidate[1..($candidate.Count - 1)]
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            & $exe @extraArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        }
    }
    return $null
}

$Python = Get-PythonCommand
if (-not $Python) {
    Write-Error "Python 3.11 or newer not found. Install it from https://www.python.org/downloads/ (or the Microsoft Store) and re-run this script."
}
$PyExe = $Python[0]
$PyArgs = $Python[1..($Python.Count - 1)]
$version = & $PyExe @PyArgs --version
Write-Host "Found $version"

# --- Virtual environment ---
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment at .venv ..."
    & $PyExe @PyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to create .venv" }
} else {
    Write-Host "Virtual environment .venv already exists."
}

Write-Host "Installing dependencies from requirements.txt ..."
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed" }

# --- ffmpeg ---
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Host "Found ffmpeg."
} else {
    Write-Host "Warning: ffmpeg not found on PATH. Install it with:"
    Write-Host "  winget install Gyan.FFmpeg"
}

# --- deno (needed for yt-dlp PO tokens) ---
$DenoUserPath = Join-Path $env:USERPROFILE ".deno\bin\deno.exe"
if ((Get-Command deno -ErrorAction SilentlyContinue) -or (Test-Path $DenoUserPath)) {
    Write-Host "Found deno."
} else {
    Write-Host "Warning: deno not found (needed for yt-dlp PO tokens). Install it with:"
    Write-Host "  winget install DenoLand.Deno"
}

# --- .env ---
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example -- fill in your Spotify credentials."
} else {
    Write-Host ".env already exists."
}

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  1. Edit .env and fill in your Spotify credentials."
Write-Host "  2. Start the server: scripts\run.ps1"
Write-Host ""
Write-Host "Note: on native Windows, Chrome cookie extraction can be blocked by"
Write-Host "Chrome's app-bound encryption. If YouTube Music Premium quality fails,"
Write-Host "set COOKIE_BROWSER=firefox in .env (with Firefox logged into"
Write-Host "music.youtube.com) or use the manual header paste flow in the app's"
Write-Host "YouTube page."
