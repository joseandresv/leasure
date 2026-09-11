# Start Leasure (or reuse the instance already running) and open it in the browser.
# Usage: scripts\launch.ps1 [-Port 8642] [-NoAppWindow] [-NoBrowser] [-Headless] [-Stop]
#
# Uses .venv\Scripts\python.exe when the repo has a native Windows install,
# otherwise starts the server inside WSL2 (WSL forwards the port to Windows
# localhost, so the browser reaches it either way). Set LEASURE_WSL_DISTRO to
# pick a distro other than the default one.
#
# By default Leasure opens in a Chrome/Edge app window and the server stops when
# that window is closed (once downloads and syncs finish). -NoAppWindow opens the
# default browser instead and leaves the server running; -Headless is for tests.
# Set LEASURE_BROWSER to the full path of the Chromium-based exe to use.
#
# While that window is open the launcher relays its YouTube/Google cookies to
# data\browser-cookies.json, which is how a server inside WSL2 (whose NAT networking
# cannot reach the window's DevTools port) still gets the login.
#
# If PowerShell refuses to run this script, allow scripts for this session first:
#   Set-ExecutionPolicy -Scope Process Bypass

param([int]$Port = 8642, [switch]$NoAppWindow, [switch]$NoBrowser, [switch]$Headless, [switch]$Stop)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Url = "http://localhost:$Port/"
$PidFile = Join-Path $RepoRoot "data\leasure.pid"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$BrowserProfile = Join-Path $RepoRoot "data\browser-profile"
$CdpFile = Join-Path $RepoRoot "data\browser-cdp.json"
$CookieFile = Join-Path $RepoRoot "data\browser-cookies.json"
$CookieRequestFile = Join-Path $RepoRoot "data\browser-cookies.request"
$CdpPort = 0
$AppBrowser = "chrome"
$CookieSyncMinutes = 5
$LastProbeError = "no response"
$ManualHint = "scripts\run.ps1 -Port $Port"
$StartedServer = $false
$IdleTimeoutMinutes = 30

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

function Remove-AppWindowFiles {
    foreach ($path in @($CdpFile, $CookieFile, $CookieRequestFile)) {
        if (Test-Path $path) { Remove-Item $path -Force }
    }
}

function Stop-LeasureServer {
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
    Remove-AppWindowFiles
}

function Get-FreeLoopbackPort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return $listener.LocalEndpoint.Port } finally { $listener.Stop() }
}

# Tells Leasure which DevTools endpoint belongs to the app window it can read the
# YouTube Music login from. Overwrites whatever an earlier window left behind.
function Write-CdpFile {
    param([int]$CdpPort, [int]$BrowserPid, [string]$Browser)

    $record = [ordered]@{
        port       = $CdpPort
        pid        = $BrowserPid
        browser    = $Browser
        started_at = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $CdpFile) | Out-Null
    [IO.File]::WriteAllText($CdpFile, ($record | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
}

function Wait-CdpReady {
    param([int]$CdpPort)

    $waited = [Diagnostics.Stopwatch]::StartNew()
    while ($waited.Elapsed.TotalSeconds -lt 15) {
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:$CdpPort/json/version" -UseBasicParsing -TimeoutSec 3 | Out-Null
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

# One Storage.getCookies over the app window's DevTools socket. PowerShell 5.1 has no
# WebSocket client cmdlet, so this drives .NET's ClientWebSocket directly.
function Get-CdpCookies {
    param([string]$WsUrl)

    $socket = [Net.WebSockets.ClientWebSocket]::new()
    $none = [Threading.CancellationToken]::None
    try {
        if (-not $socket.ConnectAsync([Uri]$WsUrl, $none).Wait(5000)) { throw "timed out opening $WsUrl" }
        $request = [Text.Encoding]::UTF8.GetBytes('{"id":1,"method":"Storage.getCookies"}')
        if (-not $socket.SendAsync([ArraySegment[byte]]::new($request, 0, $request.Length),
                                   [Net.WebSockets.WebSocketMessageType]::Text, $true, $none).Wait(5000)) {
            throw "timed out asking for the cookies"
        }
        $buffer = [ArraySegment[byte]]::new([byte[]]::new(65536))
        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        while ([DateTime]::UtcNow -lt $deadline) {
            # Events arrive on the same socket, and a cookie reply spans several frames.
            $message = [IO.MemoryStream]::new()
            do {
                $read = $socket.ReceiveAsync($buffer, $none)
                if (-not $read.Wait(10000)) { throw "the app window stopped answering" }
                $message.Write($buffer.Array, 0, $read.Result.Count)
            } while (-not $read.Result.EndOfMessage)
            $frame = [Text.Encoding]::UTF8.GetString($message.ToArray()) | ConvertFrom-Json
            if ($frame.id -eq 1) {
                if ($frame.error) { throw "the app window refused Storage.getCookies: $($frame.error.message)" }
                return $frame.result.cookies
            }
        }
        throw "no Storage.getCookies reply within 15 s"
    } finally {
        $socket.Dispose()
    }
}

# Relay the window's YouTube/Google cookies through a file. Needed because WSL2 with NAT
# networking cannot reach the DevTools port on Windows loopback, so a server inside WSL
# cannot read the window itself; the launcher already runs on the Windows side.
function Sync-BrowserCookies {
    try {
        $version = Invoke-RestMethod -Uri "http://127.0.0.1:$CdpPort/json/version" -TimeoutSec 5
        $relayed = @(foreach ($cookie in (Get-CdpCookies -WsUrl $version.webSocketDebuggerUrl)) {
            if ($cookie.domain -and ($cookie.domain.EndsWith(".youtube.com") -or $cookie.domain.EndsWith(".google.com"))) {
                [ordered]@{
                    name     = $cookie.name
                    value    = $cookie.value
                    domain   = $cookie.domain
                    path     = $cookie.path
                    expires  = $cookie.expires
                    secure   = $cookie.secure
                    httpOnly = $cookie.httpOnly
                }
            }
        })
        $record = [ordered]@{
            fetched_at = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
            browser    = $AppBrowser
            cookies    = $relayed
        }
        $tmp = "$CookieFile.tmp"
        [IO.File]::WriteAllText($tmp, ($record | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
        Move-Item -Path $tmp -Destination $CookieFile -Force
        Write-Host "Relayed $($relayed.Count) browser cookies to data\browser-cookies.json."
    } catch {
        Write-Host "Cookie relay failed (Leasure will ask again): $($_.Exception.Message)"
    }
}

# Path to a Chromium-based exe that can host an app window, or $null.
function Find-AppBrowser {
    if ($env:LEASURE_BROWSER) {
        if (Test-Path $env:LEASURE_BROWSER) { return $env:LEASURE_BROWSER }
        Write-Host "LEASURE_BROWSER ($($env:LEASURE_BROWSER)) does not exist; looking for Chrome or Edge instead."
    }
    foreach ($exe in @("chrome.exe", "msedge.exe")) {
        $appPaths = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\$exe"
        $recorded = (Get-ItemProperty -Path $appPaths -ErrorAction SilentlyContinue).'(default)'
        if ($recorded -and (Test-Path $recorded)) { return $recorded }
    }
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA) | Where-Object { $_ }
    foreach ($root in $roots) {
        foreach ($relative in @("Google\Chrome\Application\chrome.exe", "Microsoft\Edge\Application\msedge.exe")) {
            $candidate = Join-Path $root $relative
            if (Test-Path $candidate) { return $candidate }
        }
    }
    return $null
}

function Start-AppWindow {
    param([string]$Exe)

    New-Item -ItemType Directory -Force -Path $BrowserProfile | Out-Null
    # The dedicated profile is what makes the started process ours to wait on: with
    # the user's normal profile the exe hands the URL to the running browser and exits.
    # Chrome 136+ only honours --remote-debugging-port when the profile is not the
    # default one, which the line above already guarantees.
    $cdpPort = Get-FreeLoopbackPort
    $browserArgs = @("--app=$Url", "--user-data-dir=$BrowserProfile", "--no-first-run",
                     "--no-default-browser-check", "--window-size=1280,900",
                     "--remote-debugging-port=$cdpPort")
    if ($Headless) { $browserArgs += "--headless=new" }
    $proc = Start-Process -FilePath $Exe -ArgumentList $browserArgs -PassThru
    $script:CdpPort = $cdpPort
    $script:AppBrowser = [IO.Path]::GetFileNameWithoutExtension($Exe).ToLower()
    Write-CdpFile -CdpPort $cdpPort -BrowserPid $proc.Id -Browser $script:AppBrowser
    if (Wait-CdpReady -CdpPort $cdpPort) {
        Write-Host "App window ready (DevTools on 127.0.0.1:$cdpPort)."
        Sync-BrowserCookies
    } else {
        Write-Host "App window did not answer on 127.0.0.1:$cdpPort; YouTube Music will need the manual header paste."
    }
    return $proc
}

# Block until the app window closes, relaying its cookies on request and every few minutes.
function Wait-AppWindowClosed {
    param([Diagnostics.Process]$Proc)

    $lastSync = [DateTime]::UtcNow
    while (-not $Proc.HasExited) {
        if (Test-Path $CookieRequestFile) {
            Sync-BrowserCookies
            $lastSync = [DateTime]::UtcNow
            Remove-Item $CookieRequestFile -Force -ErrorAction SilentlyContinue
        } elseif (([DateTime]::UtcNow - $lastSync).TotalMinutes -ge $CookieSyncMinutes) {
            Sync-BrowserCookies
            $lastSync = [DateTime]::UtcNow
        }
        Start-Sleep -Seconds 2
    }
}

# Block while the server still reports downloads or a sync in flight.
function Wait-LeasureIdle {
    $waited = [Diagnostics.Stopwatch]::StartNew()
    while ($waited.Elapsed.TotalMinutes -lt $IdleTimeoutMinutes) {
        try {
            $queue = Invoke-RestMethod -Uri ($Url + "api/downloads/queue") -TimeoutSec 10
        } catch {
            return  # server gone or unreachable: nothing left to wait for
        }
        if (-not $queue.active) { return }
        Write-Host "Work still in progress; keeping Leasure up (re-checking in 10 s)."
        Start-Sleep -Seconds 10
    }
    Write-Host "Still busy after $IdleTimeoutMinutes minutes; stopping anyway."
}

if ($Stop) {
    Stop-LeasureServer
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
    $StartedServer = $true
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
    $StartedServer = $true
}

$deadline = 30
$waited = [Diagnostics.Stopwatch]::StartNew()
while ($waited.Elapsed.TotalSeconds -lt $deadline) {
    if (Test-LeasureServer) {
        Write-Host "Leasure is at $Url"
        if ($NoBrowser) { exit 0 }

        $browser = if ($NoAppWindow) { $null } else { Find-AppBrowser }
        if (-not $browser) {
            if (-not $NoAppWindow) { Write-Host "No Chrome or Edge found; opening your default browser." }
            Start-Process $Url
            Write-Host "The server keeps running after you close the tab. Stop it with:"
            Write-Host "  scripts\launch.ps1 -Stop -Port $Port"
            exit 0
        }

        $window = Start-AppWindow -Exe $browser
        if (-not $StartedServer) { Write-Host "Leasure was already running, so it stays up after the window closes." }
        Wait-AppWindowClosed -Proc $window
        Write-Host "App window closed."
        Remove-AppWindowFiles
        if ($StartedServer) {
            Wait-LeasureIdle
            Stop-LeasureServer
        }
        exit 0
    }
    Start-Sleep -Seconds 1
}

Write-Host "Leasure did not answer at $Url within $deadline seconds. Last error: $LastProbeError"
Write-Host "Start it by hand to see why:"
Write-Host "  $ManualHint"
exit 1
