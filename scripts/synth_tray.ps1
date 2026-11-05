<#
.SYNOPSIS
    Notification-area (tray) icon for a native Windows SyntH install.

.DESCRIPTION
    Started by scripts/start_synth.py. A native install launches SyntH as a
    windowless background process, so the launcher's window closes and the machine
    looks like it did nothing: the user is left staring at a desktop with no sign
    that anything started. This icon is that sign. It appears immediately, says it
    is starting, turns into "running" once the WebUI answers, and gives right-click
    access to the WebUI, a restart, a shutdown, and (later) an update.

    Everything it does is a call into the launcher, so there is one implementation
    of starting, stopping and opening SyntH: this file owns only the icon.

.NOTES
    Windows PowerShell 5.1 compatible. No dependencies beyond the .NET framework.
    Every step is appended to <AppRoot>\logs\tray.log, because a tray icon that does
    not appear has no other way of saying why. Exits immediately if another tray icon
    for this install is already running.
#>
[CmdletBinding()]
param(
    # Install root. Derived from this script's location when not supplied.
    [string]$AppRoot = '',
    # .env to read the WebUI host, ports and TLS flag from.
    [string]$EnvFile = '',
    # How often to check whether SyntH is answering, in seconds.
    [int]$PollSeconds = 2,
    # Where to append what happened. Defaults to <AppRoot>\logs\tray.log.
    [string]$LogFile = '',
    # How many checks in a row may go unanswered before SyntH counts as stopped. Two
    # keeps a WebUI that is busy for one poll from being mistaken for a dead one.
    [int]$StopMissLimit = 2,
    # Seconds to wait for a requested stop to take effect before saying it did not.
    [int]$StopGraceSeconds = 20,
    # Seconds to wait for a restarted SyntH to answer before giving up on it.
    [int]$RestartGraceSeconds = 120,
    # Seconds to wait for a first start before removing an icon nothing will use.
    [int]$StartupGraceSeconds = 180,
    # Skip the "starting" balloon. The icon and its menu still appear.
    [switch]$NoBalloon
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Resolve the install first: the log lives under it, so a bad AppRoot is the one
# error that cannot be logged anywhere sensible.
# ---------------------------------------------------------------------------
try {
    if (-not $AppRoot) { $AppRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
    $AppRoot = [System.IO.Path]::GetFullPath($AppRoot)
} catch {
    Write-Error "synth_tray: cannot resolve the install directory: $($_.Exception.Message)"
    exit 2
}
if (-not $EnvFile) { $EnvFile = Join-Path $AppRoot '.env' }
if (-not $LogFile) { $LogFile = Join-Path $AppRoot 'logs\tray.log' }
if ($PollSeconds -lt 1) { $PollSeconds = 1 }

function Write-TrayLog {
    param([string]$Message, [string]$Level = 'INFO')
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $line = "[$stamp] [$Level] $Message"
    $dir = Split-Path -Parent $LogFile
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            if ($dir -and -not (Test-Path -LiteralPath $dir)) {
                New-Item -ItemType Directory -Path $dir -Force | Out-Null
            }
            Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 -ErrorAction Stop
            return
        } catch {
            # A reader holding the file open, an editor, a backup: any of them can
            # refuse the append for a moment. Swallowing the line entirely is how a
            # tray that reached its message loop looked like one that never got there,
            # so retry, and if the file stays unwritable put the line on stdout, which
            # the launcher captures into logs\tray.out.log.
            Start-Sleep -Milliseconds 40
        }
    }
    Write-Output $line
}

Write-TrayLog "--- tray start: pid=$PID app=$AppRoot ps=$($PSVersionTable.PSVersion) ---"

trap {
    # A tray that throws must say so: otherwise the user is left with an empty
    # notification area and no explanation anywhere. This is why the log exists.
    Write-TrayLog "FATAL $($_.Exception.GetType().Name): $($_.Exception.Message)" 'ERROR'
    Write-TrayLog ($_.ScriptStackTrace) 'ERROR'
    try { [System.Windows.Forms.Application]::ExitThread() } catch { }
    exit 1
}

# ---------------------------------------------------------------------------
# Where the WebUI is, read the same way the health check reads it.
# ---------------------------------------------------------------------------
function Read-DotEnv {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in (Get-Content -LiteralPath $Path)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            # Snapshot the groups before anything else: -match and -notmatch both
            # overwrite the automatic $matches, so reading it after another regex
            # operation indexes into whatever ran last.
            $key = $matches[1]
            $value = $matches[2].Trim()
            # Keep a '#' when it is inside quotes; strip an inline comment otherwise.
            if ($value -notmatch '^\s*[''"]') { $value = ($value -split '\s+#')[0].Trim() }
            $map[$key] = $value.Trim('"').Trim("'")
        }
    }
    return $map
}

$settings = Read-DotEnv -Path $EnvFile
$tls = ($settings['SYNTH_WEBUI_TLS'] -eq '1') -or ($settings['SECURE_CONNECTION'] -eq '1')
$webHost = if ($settings['SYNTH_WEBUI_HOST']) { $settings['SYNTH_WEBUI_HOST'] } else { '127.0.0.1' }
$httpPort = if ($settings['SYNTH_WEBUI_HTTP_PORT']) { $settings['SYNTH_WEBUI_HTTP_PORT'] } else { '8080' }
$httpsPort = if ($settings['SYNTH_WEBUI_HTTPS_PORT']) { $settings['SYNTH_WEBUI_HTTPS_PORT'] } else { '8000' }

if ($tls) {
    $script:PrimaryUrl = 'https://{0}:{1}/' -f $webHost, $httpsPort
    $script:OtherUrl = 'http://{0}:{1}/' -f $webHost, $httpPort
} else {
    $script:PrimaryUrl = 'http://{0}:{1}/' -f $webHost, $httpPort
    $script:OtherUrl = 'https://{0}:{1}/' -f $webHost, $httpsPort
}
Write-TrayLog "env: $EnvFile (exists: $(Test-Path -LiteralPath $EnvFile)); tls=$tls; urls: $($script:PrimaryUrl), $($script:OtherUrl)"

# ---------------------------------------------------------------------------
# WinForms. Reported on its own line: a missing assembly is a different problem
# from a tray that cannot reach the app.
# ---------------------------------------------------------------------------
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Write-TrayLog 'WinForms loaded'
} catch {
    Write-TrayLog "FATAL cannot load WinForms: $($_.Exception.Message)" 'ERROR'
    exit 3
}

# ---------------------------------------------------------------------------
# Only one tray icon per install: a second launch must not add a second icon.
# ---------------------------------------------------------------------------
$mutexName = 'Local\SyntH-Tray-' + [Math]::Abs($AppRoot.ToLower().GetHashCode())
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$owned = $false
try {
    $owned = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $owned = $true
}
if (-not $owned) {
    Write-TrayLog "another tray already owns '$mutexName'; exiting" 'WARN'
    exit 0
}
Write-TrayLog "mutex '$mutexName' acquired"

function Get-PythonPath {
    param([switch]$Console)
    $names = if ($Console) { @('python.exe', 'pythonw.exe') } else { @('pythonw.exe', 'python.exe') }
    foreach ($name in $names) {
        $candidate = Join-Path $AppRoot (Join-Path '.venv\Scripts' $name)
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Invoke-Launcher {
    <#
        Call the one launcher for start/stop/open.

        Never waits for it. The tray is one thread: a click that waited for
        ``start_synth.py --stop`` would hold the message loop for as long as it takes
        the application to die, and for that whole time the icon answers a right-click
        with a menu that does nothing. The state poll reports what actually happened
        instead, which is better evidence anyway, and the launcher's own account of the
        stop is in logs\synth_launch.log.

        Always the windowless interpreter: nothing should flash a console window on a
        desktop launch.
    #>
    param([string[]]$Arguments, [string]$Description = 'launch')
    $python = Get-PythonPath -Console:$false
    if (-not $python) {
        Write-TrayLog "action '$Description': no interpreter under $AppRoot\.venv\Scripts" 'ERROR'
        return
    }
    $launcher = Join-Path $AppRoot 'scripts\start_synth.py'
    Write-TrayLog "action '$Description': $python $launcher $($Arguments -join ' ')"
    try {
        Start-Process -FilePath $python `
            -ArgumentList (@($launcher) + $Arguments) `
            -WindowStyle Hidden
    } catch {
        Write-TrayLog "action '$Description' failed: $($_.Exception.Message)" 'ERROR'
    }
}

function Test-SyntHIsUp {
    # Returns the URL that answered, or $null.
    foreach ($url in @($script:PrimaryUrl, $script:OtherUrl)) {
        try {
            $request = [System.Net.HttpWebRequest]::Create($url)
            $request.Method = 'GET'
            $request.Timeout = 1500
            $request.ReadWriteTimeout = 1500
            $request.ServerCertificateValidationCallback = { $true }
            $request.AllowAutoRedirect = $true
            $response = $request.GetResponse()
            $response.Close()
            return $url
        } catch {
            continue
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# One state check: probe, then follow it with the tooltip, the log, and whether
# this icon should still be here at all. Called on every tick. It is the tray's
# only clock, so it must never block: a menu click that waited for SyntH to die
# would freeze the icon that was clicked.
# ---------------------------------------------------------------------------
function Set-PollInterval {
    param([int]$Milliseconds)
    try { if ($script:timer) { $script:timer.Interval = $Milliseconds } } catch { }
}

function Stop-Tray {
    <#
        Say why, take the icon away, and end the message loop.

        An icon that outlives the application is worse than no icon: Windows still
        shows it, a right-click still opens the menu, and every entry in that menu is
        about an application that is not there.
    #>
    param([string]$Reason, [string]$Level = 'INFO')
    Write-TrayLog "removing the icon: $Reason" $Level
    if (-not $NoBalloon) {
        try {
            $notify.ShowBalloonTip(5000, 'SyntH', $Reason, [System.Windows.Forms.ToolTipIcon]::Info)
        } catch { }
    }
    try { $notify.Visible = $false } catch { }
    [System.Windows.Forms.Application]::ExitThread()
}

function Update-State {
    try {
        $url = Test-SyntHIsUp
        if ($url) {
            $script:Misses = 0
            if ($script:WatchStopUntil) {
                # A stop was asked for and SyntH is still answering. That is expected for
                # the few seconds it takes to go down, so the watch keeps running until the
                # grace is up; only then is it a stop that did not take effect, and the
                # person who asked has to be told.
                if ((Get-Date) -gt $script:WatchStopUntil) {
                    $script:WatchStopUntil = $null
                    Set-PollInterval ($PollSeconds * 1000)
                    Write-TrayLog 'the stop did not take effect: the WebUI is still answering' 'ERROR'
                    $notify.Text = 'SyntH is still running'
                    if (-not $NoBalloon) {
                        $notify.ShowBalloonTip(7000, 'SyntH is still running', "The stop request did not take effect. See logs\tray.log and logs\synth_launch.log.", [System.Windows.Forms.ToolTipIcon]::Warning)
                    }
                }
            }
            if ($script:State -ne 'running') {
                $cameBack = $script:Restarting
                $script:Restarting = $false
                $script:EverRunning = $true
                $script:State = 'running'
                $script:ReadyUrl = $url
                $notify.Text = 'SyntH is running'
                Write-TrayLog "state: running ($url)"
                if (-not $NoBalloon) {
                    # The install's own reassurance: the launcher window is long gone.
                    $title = if ($cameBack) { 'SyntH is back' } else { 'SyntH is running' }
                    $notify.ShowBalloonTip(5000, $title, "$url`nRight-click this icon for the menu.", [System.Windows.Forms.ToolTipIcon]::Info)
                }
            }
            return
        }

        # Not answering.
        if ($script:State -eq 'running') {
            $script:Misses++
            Write-TrayLog "no answer from the WebUI ($($script:Misses) check(s) in a row)"
            if ($script:Misses -ge $StopMissLimit) {
                $script:State = 'stopped'
                $notify.Text = 'SyntH is stopped'
                Write-TrayLog 'state: stopped'
            }
        }
        if ($script:State -ne 'stopped') {
            # Still starting. Nothing to report until either it answers or this gives up.
            if (-not $script:EverRunning -and (Get-Date) -gt $script:StartedAt.AddSeconds($StartupGraceSeconds)) {
                Write-TrayLog "SyntH never answered within $StartupGraceSeconds s" 'ERROR'
                Stop-Tray "SyntH did not start. See logs\tray.log and logs\synth_bootstrap.log." 'ERROR'
            }
            return
        }

        # Stopped, and it was this icon's application: the icon goes with it. A restart
        # in flight is the one case where that would be wrong, and it has its own grace.
        if ($script:Restarting) {
            if ((Get-Date) -gt $script:RestartUntil) {
                $script:Restarting = $false
                Write-TrayLog 'the restart never came back' 'ERROR'
                Stop-Tray "SyntH was restarted but did not come back. See logs\tray.log." 'ERROR'
            }
            return
        }
        Stop-Tray 'SyntH is not running any more'
    } catch {
        # Never let a failed probe kill the icon; the next check tries again.
        Write-TrayLog "state check failed: $($_.Exception.Message)" 'WARN'
    }
}

# ---------------------------------------------------------------------------
# The icon
# ---------------------------------------------------------------------------
function Get-TrayIcon {
    foreach ($name in @('synth-tray.ico', 'synth.ico')) {
        $path = Join-Path $AppRoot (Join-Path 'installer' $name)
        if (Test-Path -LiteralPath $path) {
            try {
                $icon = New-Object System.Drawing.Icon($path)
                Write-TrayLog "icon: installer\$name ($($icon.Width)x$($icon.Height))"
                return $icon
            } catch {
                Write-TrayLog "icon installer\$name failed to load: $($_.Exception.Message)" 'WARN'
            }
        } else {
            Write-TrayLog "icon candidate missing: installer\$name" 'WARN'
        }
    }
    Write-TrayLog 'falling back to the built-in application icon' 'WARN'
    return [System.Drawing.SystemIcons]::Application
}

$script:State = 'starting'
$script:ReadyUrl = $null
# Whether this tray has ever seen SyntH answer. Until it has, a silent WebUI is a start
# in progress; after it has, a silent WebUI is a stopped application.
$script:EverRunning = $false
$script:Misses = 0
$script:StartedAt = Get-Date
# Set while a requested stop is being watched, and while a restart is in flight: both
# keep this icon alive through a period where SyntH is legitimately not answering.
$script:WatchStopUntil = $null
$script:Restarting = $false
$script:RestartUntil = $null

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = Get-TrayIcon
$notify.Visible = $true
$notify.Text = 'SyntH is starting'
Write-TrayLog 'notification icon created and made visible'

$menu = New-Object System.Windows.Forms.ContextMenuStrip

$openItem = New-Object System.Windows.Forms.ToolStripMenuItem('Open SyntH')
$openItem.Font = New-Object System.Drawing.Font($openItem.Font, [System.Drawing.FontStyle]::Bold)
$openItem.Add_Click({
    # The launcher opens the WebUI when SyntH is up, and starts it when it is not.
    Invoke-Launcher -Arguments @('--no-tray') -Description 'open the WebUI'
}) | Out-Null
$menu.Items.Add($openItem) | Out-Null

$restartItem = New-Object System.Windows.Forms.ToolStripMenuItem('Restart')
$restartItem.Add_Click({
    try {
        Write-TrayLog 'action: restart'
        $notify.Text = 'SyntH is restarting'
        # Both of these hand the work to the launcher and return at once. The tray has
        # one thread, so anything that waited here would freeze the icon and its menu
        # for as long as the application took to go down and come back.
        $script:State = 'running'
        $script:Misses = 0
        $script:Restarting = $true
        $script:RestartUntil = (Get-Date).AddSeconds($RestartGraceSeconds)
        Set-PollInterval 1000
        Invoke-Launcher -Arguments @('--stop') -Description 'restart: stop'
        Invoke-Launcher -Arguments @('--no-browser', '--no-tray') -Description 'restart: start again'
    } catch {
        Write-TrayLog "restart failed: $($_.Exception.Message)" 'ERROR'
    }
}) | Out-Null
$menu.Items.Add($restartItem) | Out-Null

$shutdownItem = New-Object System.Windows.Forms.ToolStripMenuItem('Shut down')
$shutdownItem.Add_Click({
    try {
        Write-TrayLog 'action: shut down'
        $notify.Text = 'SyntH is shutting down'
        $script:State = 'running'
        $script:Misses = 0
        $script:WatchStopUntil = (Get-Date).AddSeconds($StopGraceSeconds)
        Set-PollInterval 1000
        Invoke-Launcher -Arguments @('--stop') -Description 'shut down'
        # From here the state poll reports the outcome: the icon is removed once SyntH
        # stops answering, and if it is still answering after the grace the tooltip and
        # a balloon say that the stop did not take effect.
    } catch {
        Write-TrayLog "shutdown failed: $($_.Exception.Message)" 'ERROR'
    }
}) | Out-Null
$menu.Items.Add($shutdownItem) | Out-Null

$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

# Placeholder so the shape of the menu is already right: updating an install is
# not implemented yet, and a greyed entry says that better than a missing one.
$updateItem = New-Object System.Windows.Forms.ToolStripMenuItem('Check for updates')
$updateItem.Enabled = $false
$updateItem.ToolTipText = 'Not implemented yet'
$menu.Items.Add($updateItem) | Out-Null

$quitItem = New-Object System.Windows.Forms.ToolStripMenuItem('Hide this icon')
$quitItem.ToolTipText = 'SyntH keeps running'
$quitItem.Add_Click({
    Write-TrayLog 'action: hide requested'
    $script:State = 'hidden'
    $notify.Visible = $false
    [System.Windows.Forms.Application]::ExitThread()
}) | Out-Null
$menu.Items.Add($quitItem) | Out-Null

$notify.ContextMenuStrip = $menu
$notify.Add_MouseDoubleClick({ $openItem.PerformClick() })
Write-TrayLog 'menu built: open / restart / shut down / check for updates (disabled) / hide'

# ---------------------------------------------------------------------------
# State poll: the tooltip, the balloon and whether this icon should still exist
# all follow it. It is script-scoped so an action can retime it while watching a
# stop or a restart.
# ---------------------------------------------------------------------------
$script:timer = New-Object System.Windows.Forms.Timer
$script:timer.Interval = $PollSeconds * 1000
$script:timer.Add_Tick({ Update-State })
$script:timer.Start()

# Announce the launch straight away: this is the whole point of the icon.
if (-not $NoBalloon) {
    try {
        $notify.ShowBalloonTip(5000, 'SyntH is starting', 'It will be ready in a moment. Right-click this icon for the menu.', [System.Windows.Forms.ToolTipIcon]::Info)
        Write-TrayLog 'start balloon shown'
    } catch {
        Write-TrayLog "start balloon failed: $($_.Exception.Message)" 'WARN'
    }
}

Write-TrayLog "entering the message loop (icon visible: $($notify.Visible))"

try {
    [System.Windows.Forms.Application]::Run()
} finally {
    Write-TrayLog 'message loop ended'
    try { $script:timer.Stop() } catch { }
    try { $notify.Visible = $false; $notify.Dispose() } catch { }
    try { $mutex.ReleaseMutex(); $mutex.Dispose() } catch { }
}
