# SyntH SearXNG provisioner for native Windows runtimes.
#
# The Docker image ships SearXNG in-container (see Dockerfile + container/s6-services/
# searxng/run). A native Windows runtime has no container, so this script mirrors the
# same canonical install locally and runs it on 127.0.0.1:8888 - which is exactly what
# the web_search plugin's default SEARXNG_URL already points at, so no config change is
# needed afterwards.
#
# Usage:
#   .\scripts\searxng_windows.ps1 install    # clone + install + start (default)
#   .\scripts\searxng_windows.ps1 start
#   .\scripts\searxng_windows.ps1 status
#   .\scripts\searxng_windows.ps1 stop
#   .\scripts\searxng_windows.ps1 restart
#   .\scripts\searxng_windows.ps1 update     # re-clone latest + reinstall + restart
#   .\scripts\searxng_windows.ps1 install -NoStart   # install only, don't auto-start
#   .\scripts\searxng_windows.ps1 install -RegisterStartup  # + logon auto-start task
#
# Requirements: git and uv on PATH (install_prereqs.ps1 installs uv; git is required
# for the clone). Everything else is provisioned into the runtime root.

param(
    [ValidateSet("install", "start", "stop", "status", "restart", "update")]
    [string]$Action = "install",
    [string]$RuntimeRoot = "",
    [string]$BindHost = "127.0.0.1",
    [int]$BindPort = 8888,
    [switch]$NoStart,
    [switch]$RegisterStartup
)

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$RepoRoot = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { (Get-Location).Path }
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $RepoRoot "plugins\web_search\searxng-runtime"
}

$SettingsPath = Join-Path $RepoRoot "container\searxng\settings.yml"
$SrcDir   = Join-Path $RuntimeRoot "src"
$VenvDir  = Join-Path $RuntimeRoot "venv"
$PidFile  = Join-Path $RuntimeRoot "searxng.pid"
$OutLog   = Join-Path $RuntimeRoot "searxng.out.log"
$ErrLog   = Join-Path $RuntimeRoot "searxng.err.log"
$LogDir   = Join-Path $RepoRoot "logs"
$AppLog   = Join-Path $LogDir "searxng_windows.log"
New-Item -Force -ItemType Directory -Path $LogDir -ErrorAction SilentlyContinue | Out-Null

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $AppLog -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}

function Assert-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Log "ERROR: uv not found on PATH. Install it first (install_prereqs.ps1 does this):"
        Log "       powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`""
        exit 1
    }
    return $cmd.Source
}

function Get-RunningPid {
    if (Test-Path $PidFile) {
        $pidText = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
        $parsed = 0
        if ([int]::TryParse($pidText, [ref]$parsed) -and $parsed -gt 0) { return $parsed }
    }
    return 0
}

function Test-PortListening {
    param([int]$Port)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $task = $client.ConnectAsync($BindHost, $Port)
        if ($task.Wait(1500) -and $client.Connected) {
            $client.Close()
            return $true
        }
        $client.Close()
        return $false
    } catch {
        return $false
    }
}

function Assert-Installed {
    if (-not (Test-Path (Join-Path $VenvDir "Scripts\granian.exe"))) {
        Log "ERROR: SearXNG is not installed yet. Run: .\scripts\searxng_windows.ps1 install"
        exit 1
    }
}

function Reset-Source {
    if (Test-Path $SrcDir) {
        Remove-Item $SrcDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Log "Cloning searxng/searxng into $SrcDir ..."
    git clone --depth 1 --no-checkout https://github.com/searxng/searxng.git $SrcDir 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log "ERROR: git clone failed (exit $LASTEXITCODE)."
        exit 1
    }
    # The upstream repo ships template files whose names contain a colon
    # (e.g. 'utils/templates/.../searxng.conf:socket'), which NTFS forbids, so
    # the normal clone checkout fatals on Windows. Restoring skips exactly
    # those paths (harmless packaging examples) and materializes the rest.
    git -C $SrcDir restore --source=HEAD :/ 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log "ERROR: source checkout restore failed (exit $LASTEXITCODE)."
        exit 1
    }
}

function Apply-WindowsPatch {
    # SearXNG imports the Unix-only 'pwd' module at the top of
    # searx/valkeydb.py, so the app cannot import on Windows. With the shipped
    # settings (limiter off, no valkey/redis URL) the only 'pwd' use - the
    # valkey connect-error log - is never reached, so a guarded import is the
    # minimal, safe compatibility patch. It is applied to the private runtime
    # copy only (gitignored), never to upstream.
    $valkeyDb = Join-Path $SrcDir "searx\valkeydb.py"
    if (-not (Test-Path $valkeyDb)) {
        Log "WARN: searx/valkeydb.py not found - skipping Windows pwd patch."
        return
    }
    $content = [System.IO.File]::ReadAllText($valkeyDb)

    $importMarker = "except ImportError:  # Windows has no pwd module"
    # The corrected block indents _pw under the new 'if' (12 spaces). Checking
    # BOTH markers lets a half-applied patch (import guarded, block still at the
    # old 8-space indent) be repaired instead of being skipped or re-wrapped.
    $blockMarker = '            _pw = pwd.getpwuid(os.getuid())'
    if ($content.Contains($importMarker) -and $content.Contains($blockMarker)) {
        Log "Windows pwd patch already applied - skipping."
        return
    }

    # git's autocrlf may have checked the tree out with CRLF endings on
    # Windows; normalize to LF so the literal replacements below always match.
    $content = $content.Replace("`r`n", "`n")

    if (-not $content.Contains($importMarker)) {
        $guardedImport = "try:`n    import pwd`nexcept ImportError:  # Windows has no pwd module`n    pwd = None`n"
        $content = $content.Replace("import pwd`n", $guardedImport)
    }

    $oldLine1 = '        _pw = pwd.getpwuid(os.getuid())'
    $oldLine2 = '        logger.exception("[%s (%s)] can''t connect valkey DB ...", _pw.pw_name, _pw.pw_uid)'
    $oldBlock = $oldLine1 + "`n" + $oldLine2
    $newBlock = (
        '        if pwd is not None:' + "`n" +
        '            ' + $oldLine1.Trim() + "`n" +
        '            ' + $oldLine2.Trim() + "`n" +
        '        else:' + "`n" +
        '            logger.exception("can''t connect valkey DB ...")'
    )
    $content = $content.Replace($oldBlock, $newBlock)

    if (-not $content.Contains($importMarker) -or -not $content.Contains($blockMarker)) {
        Log "ERROR: Windows pwd patch could not be applied (source changed upstream?)."
        exit 1
    }
    [System.IO.File]::WriteAllText($valkeyDb, $content)
    Log "Applied Windows pwd compatibility patch to searx/valkeydb.py"
}

function Invoke-Install {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Log "ERROR: git not found on PATH - required to clone SearXNG."
        exit 1
    }
    $uv = Assert-Uv
    New-Item -Force -ItemType Directory -Path $RuntimeRoot | Out-Null

    # 1. Source checkout - mirrors the Docker COPY clone. Key off the packaging
    #    file, not .git: an interrupted earlier clone can leave an empty worktree.
    if (-not (Test-Path (Join-Path $SrcDir "setup.py"))) {
        Reset-Source
    } else {
        Log "Source already present at $SrcDir - skipping clone."
    }

    # 2. venv (matches the Docker install: setuptools/wheel + requirements + granian,
    #    then an editable install with --no-build-isolation).
    if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
        Log "Creating venv at $VenvDir ..."
        & $uv venv $VenvDir 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Log "ERROR: uv venv failed (exit $LASTEXITCODE)."
            exit 1
        }
    }

    $pyExe = Join-Path $VenvDir "Scripts\python.exe"
    Log "Installing SearXNG dependencies (this can take a few minutes)..."
    & $uv pip install --python $pyExe setuptools wheel -r (Join-Path $SrcDir "requirements.txt") granian 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log "ERROR: dependency install failed (exit $LASTEXITCODE). See $AppLog."
        exit 1
    }
    & $uv pip install --python $pyExe --no-build-isolation -e $SrcDir 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log "ERROR: editable SearXNG install failed (exit $LASTEXITCODE)."
        exit 1
    }

    Apply-WindowsPatch

    # 3. Windows-server fallback: granian is the container server; on Windows the
    #    wsgi entry works too, but if its executable is missing fall back to
    #    waitress (a pure-Python WSGI server that is rock-solid on Windows).
    $serverExe = Join-Path $VenvDir "Scripts\granian.exe"
    if (-not (Test-Path $serverExe)) {
        Log "granian not available on Windows - installing waitress as fallback server."
        & $uv pip install --python $pyExe waitress 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Log "ERROR: waitress install failed (exit $LASTEXITCODE)."
            exit 1
        }
    }

    Log "Install complete at $RuntimeRoot (settings: $SettingsPath)"
}

function Invoke-Start {
    Assert-Installed

    $existing = Get-RunningPid
    if ($existing -and (Get-Process -Id $existing -ErrorAction SilentlyContinue)) {
        Log "Already running (PID $existing)."
        return
    }
    if (Test-PortListening $BindPort) {
        Log "Port $BindPort is already serving - nothing to do."
        return
    }

    if (-not (Test-Path $SettingsPath)) {
        Log "ERROR: settings file not found at $SettingsPath"
        exit 1
    }

    $granian = Join-Path $VenvDir "Scripts\granian.exe"
    $waitress = Join-Path $VenvDir "Scripts\waitress-serve.exe"
    $serverCmd = $granian
    $serverArgs = @("--interface", "wsgi", "--host", $BindHost, "--port", "$BindPort", "searx.webapp:application")
    if (-not (Test-Path $granian)) {
        $serverCmd = $waitress
        $serverArgs = @("--listen=$BindHost`:$BindPort", "searx.webapp:application")
    }

    Log "Starting SearXNG on $BindHost`:$BindPort ..."
    $oldSettings = $env:SEARXNG_SETTINGS_PATH
    $env:SEARXNG_SETTINGS_PATH = $SettingsPath
    try {
        $proc = Start-Process -FilePath $serverCmd `
            -ArgumentList $serverArgs `
            -WorkingDirectory $SrcDir `
            -WindowStyle Hidden `
            -RedirectStandardOutput $OutLog `
            -RedirectStandardError $ErrLog `
            -PassThru
        Set-Content -Path $PidFile -Value $proc.Id
        Log "Started (PID $($proc.Id)). Logs: $OutLog / $ErrLog"
    } catch {
        Log "ERROR: failed to start server: $_"
        exit 1
    } finally {
        $env:SEARXNG_SETTINGS_PATH = $oldSettings
    }

    # Health check: poll the JSON search endpoint until the first query answers.
    # The first request can take a while as the engines warm up.
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 2
        if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
            Log "ERROR: server process exited early. Tail of stderr:"
            if (Test-Path $ErrLog) { Get-Content $ErrLog -Tail 10 | ForEach-Object { Log "  $_" } }
            exit 1
        }
        try {
            $resp = Invoke-WebRequest -Uri "http://$BindHost`:$BindPort/search?q=test&format=json" `
                -Headers @{ "User-Agent" = "SyntH-WebSearch/1.0 (self-hosted persona system)" } `
                -TimeoutSec 10 -UseBasicParsing
            if ($resp.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            # Not ready yet - keep polling.
        }
    }

    if ($ready) {
        Log "OK: SearXNG answered on http://$BindHost`:$BindPort/search (format=json)."
    } else {
        Log "WARNING: started but did not answer within ~120s. Check $ErrLog."
    }
}

function Get-SearxngProcessIds {
    # Granian forks a python multiprocessing worker that owns the real listener;
    # it never appears in the pid file and its command line carries no searx
    # marker (just spawn_main + --multiprocessing-fork). Identify both:
    #   - the launcher: command line references the runtime src/venv or searx.webapp
    #   - the worker:   executable lives under the venv's python home (pyvenv.cfg)
    #                   AND the command line has --multiprocessing-fork
    $ids = @()
    try {
        $homeMatch = ""
        $cfg = Join-Path $VenvDir "pyvenv.cfg"
        if (Test-Path $cfg) {
            $homeLine = Get-Content $cfg | Where-Object { $_ -match "^home\s*=" } |
                Select-Object -First 1
            if ($homeLine) { $homeMatch = ($homeLine -split "=", 2)[1].Trim() }
        }
        Get-CimInstance Win32_Process | ForEach-Object {
            $cl = [string]$_.CommandLine
            if (-not $cl) { return }
            $exe = [string]$_.ExecutablePath
            $isLauncher = ($cl.Contains($SrcDir)) -or ($cl.Contains($VenvDir)) -or
                ($cl.Contains("searx.webapp"))
            $isWorker = $cl.Contains("--multiprocessing-fork") -and $homeMatch -and
                $exe.StartsWith($homeMatch, [System.StringComparison]::OrdinalIgnoreCase)
            if ($isLauncher -or $isWorker) { $ids += [int]$_.ProcessId }
        }
    } catch {
        Log "WARN: could not enumerate SearXNG processes: $_"
    }
    return $ids
}

function Invoke-Stop {
    # Granian spawns a worker subprocess that owns the actual listener, so
    # killing the launcher PID alone orphans the server. Kill the whole tree
    # (taskkill /T) plus anything still listening on the bind port.
    $targets = @()
    $pidText = Get-RunningPid
    if ($pidText) { $targets += $pidText }
    $targets += Get-SearxngProcessIds
    Get-NetTCPConnection -LocalPort $BindPort -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { $targets += [int]$_.OwningProcess }

    $unique = $targets | Sort-Object -Unique
    foreach ($t in $unique) {
        taskkill /PID $t /T /F 2>&1 | Out-Null
    }
    if ($unique.Count -gt 0) {
        Log "Stopped (PID(s): $($unique -join ', '))."
    } else {
        Log "Not running."
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    # Give the OS a moment to release the port before a subsequent start.
    Start-Sleep -Milliseconds 500
}

function Invoke-Status {
    $pidText = Get-RunningPid
    $running = $pidText -and (Get-Process -Id $pidText -ErrorAction SilentlyContinue)
    $listening = Test-PortListening $BindPort
    if ($running)    { Log "SearXNG process: running (PID $pidText)" }
    else             { Log "SearXNG process: not running" }
    if ($listening)  { Log "SearXNG is listening on $BindHost`:$BindPort" }
    else             { Log "SearXNG is NOT listening on $BindHost`:$BindPort" }
    if ($running -and $listening) {
        Log "STATUS: OK - http://$BindHost`:$BindPort/search?q=test&format=json"
    } else {
        Log "STATUS: DOWN - run '.\scripts\searxng_windows.ps1 start'"
        exit 1
    }
}

function Invoke-Update {
    Assert-Installed
    Log "Refreshing SearXNG source (re-clone; upstream ships NTFS-invalid filenames, so pull/reset are unusable on Windows)..."
    Invoke-Stop
    Reset-Source
    $pyExe = Join-Path $VenvDir "Scripts\python.exe"
    $uv = Assert-Uv
    Log "Reinstalling dependencies..."
    & $uv pip install --python $pyExe setuptools wheel -r (Join-Path $SrcDir "requirements.txt") granian 2>&1 | Out-Null
    & $uv pip install --python $pyExe --no-build-isolation -e $SrcDir 2>&1 | Out-Null
    Apply-WindowsPatch
    Invoke-Start
}

function Invoke-RegisterStartup {
    $taskName = "SyntH-SearXNG"
    $scriptPath = Join-Path $PSScriptRoot "searxng_windows.ps1"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" start"
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Description "Starts the SyntH local SearXNG instance on 127.0.0.1:8888 at logon." `
        -Force -ErrorAction SilentlyContinue
    if ($?) {
        Log "Registered startup task '$taskName' (runs 'start' at logon)."
    } else {
        Log "WARNING: could not register startup task '$taskName' (may require elevation)."
    }
}

Log "=== SearXNG ($Action) ==="
Log "Runtime root: $RuntimeRoot"

switch ($Action) {
    "install" {
        Invoke-Install
        if ($RegisterStartup) { Invoke-RegisterStartup }
        if (-not $NoStart) { Invoke-Start }
    }
    "start"   { Invoke-Start }
    "stop"    { Invoke-Stop }
    "status"  { Invoke-Status }
    "restart" { Invoke-Stop; Invoke-Start }
    "update"  { Invoke-Update }
}

Log "=== SearXNG $Action done ==="
