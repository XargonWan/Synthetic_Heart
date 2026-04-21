# SyntH prerequisite installer — runs during Inno Setup [Run] phase.
# Usage: install_prereqs.ps1 -MariaDbMsi <path-to-msi> [-InstallPostgres 1]
# Logs to logs\prereqs.log and pauses at the end.
#
# winget is user-scoped and invisible in elevated sessions — this script
# locates it explicitly and falls back to direct downloads if unavailable.

param(
    [Parameter(Mandatory=$true)]
    [string]$MariaDbMsi,
    [string]$InstallPostgres = "0"
)

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

# Guaranteed-writable log in TEMP — exists even if the app-dir path is wrong.
$TempLog = Join-Path $env:TEMP "synth_prereqs.log"

# Try to also log inside the app's logs\ dir.
$AppDir  = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
$LogDir  = Join-Path $AppDir "logs"
$AppLog  = Join-Path $LogDir "prereqs.log"
New-Item -Force -ItemType Directory -Path $LogDir -ErrorAction SilentlyContinue | Out-Null

function Log($msg) {
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $TempLog -Value $line -Encoding UTF8
    Add-Content -Path $AppLog  -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}

# Find winget — it lives in the user's AppData and is invisible to elevated processes.
# We search known locations explicitly rather than relying on PATH.
function Find-Winget {
    $cmd = Get-Command winget -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        "$env:LOCALAPPDATA\Microsoft\WindowsApps\winget.exe"
    )
    $storeDir = "$env:ProgramFiles\WindowsApps"
    if (Test-Path $storeDir) {
        $found = Get-ChildItem $storeDir -Filter "Microsoft.DesktopAppInstaller*" -Directory -ErrorAction SilentlyContinue |
                 ForEach-Object { Join-Path $_.FullName "winget.exe" } |
                 Where-Object { Test-Path $_ } |
                 Select-Object -First 1
        if ($found) { $candidates += $found }
    }
    return ($candidates | Where-Object { Test-Path $_ } | Select-Object -First 1)
}

function Invoke-Winget {
    param([string[]]$Args)
    $wg = Find-Winget
    if ($wg) {
        & $wg @Args 2>&1 | Tee-Object -Append -FilePath $LogFile
        return $LASTEXITCODE
    }
    Log "WARN: winget not found — using direct download fallback"
    return -1
}

Log "=== SyntH Prerequisite Installer ==="
Log "Log: $LogFile"
Log ""

# ── Refresh PATH ──────────────────────────────────────────────────────────────
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path","User")

# ── 1. MariaDB ────────────────────────────────────────────────────────────────
Log "--- Step 1: MariaDB ---"
if (Get-Service -Name MariaDB -ErrorAction SilentlyContinue) {
    Log "SKIP: MariaDB service already exists"
} elseif (-not (Test-Path $MariaDbMsi)) {
    Log "ERROR: MariaDB MSI not found at $MariaDbMsi"
} else {
    Log "Installing MariaDB from $MariaDbMsi ..."
    $p = Start-Process msiexec.exe `
        -ArgumentList "/i `"$MariaDbMsi`" /quiet /norestart SERVICENAME=MariaDB ALLOWREMOTELOCALROOT=1" `
        -Wait -PassThru
    if ($p.ExitCode -eq 0 -or $p.ExitCode -eq 3010) {
        Log "OK: MariaDB installed (exit $($p.ExitCode))"
    } else {
        Log "ERROR: MariaDB install failed (exit $($p.ExitCode))"
    }
}
Log ""

# ── 2. Python ─────────────────────────────────────────────────────────────────
Log "--- Step 2: Python ---"
$pyVer = (python --version 2>&1) -as [string]
if ($pyVer -match "3\.(1[1-9]|[2-9]\d)") {
    Log "SKIP: $pyVer already installed"
} else {
    $rc = Invoke-Winget @("install","-e","--id","Python.Python.3.11","--silent","--accept-package-agreements","--accept-source-agreements")
    if ($rc -ne 0) {
        # Direct download fallback
        $pyRelease = "3.11.9"
        $pyUrl  = "https://www.python.org/ftp/python/$pyRelease/python-$pyRelease-amd64.exe"
        $pyDest = Join-Path $env:TEMP "python-$pyRelease-amd64.exe"
        Log "Downloading Python $pyRelease from python.org..."
        try {
            (New-Object System.Net.WebClient).DownloadFile($pyUrl, $pyDest)
            $p = Start-Process $pyDest -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1" -Wait -PassThru
            Log "Python installed (exit $($p.ExitCode))"
        } catch {
            Log "ERROR: Python download failed: $_"
        }
    } else {
        Log "Python install done"
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
}
Log ""

# ── 3. uv ─────────────────────────────────────────────────────────────────────
Log "--- Step 3: uv ---"
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Log "SKIP: uv already installed at $((Get-Command uv).Source)"
} else {
    Log "Installing uv..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression 2>&1 | Tee-Object -Append -FilePath $LogFile
        Log "uv install done"
    } catch {
        Log "ERROR: uv install failed: $_"
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
}
Log ""

# ── 4. Node.js LTS ────────────────────────────────────────────────────────────
Log "--- Step 4: Node.js ---"
if (Get-Command node -ErrorAction SilentlyContinue) {
    Log "SKIP: Node.js already installed at $((Get-Command node).Source)"
} else {
    $rc = Invoke-Winget @("install","-e","--id","OpenJS.NodeJS.LTS","--silent","--accept-package-agreements","--accept-source-agreements")
    if ($rc -ne 0) {
        $nodeUrl  = "https://nodejs.org/dist/lts/node-lts-x64.msi"
        $nodeDest = Join-Path $env:TEMP "node-lts-x64.msi"
        Log "Downloading Node.js LTS..."
        try {
            (New-Object System.Net.WebClient).DownloadFile($nodeUrl, $nodeDest)
            $p = Start-Process msiexec.exe -ArgumentList "/i `"$nodeDest`" /quiet /norestart" -Wait -PassThru
            Log "Node.js installed (exit $($p.ExitCode))"
        } catch {
            Log "ERROR: Node.js download failed: $_"
        }
    } else {
        Log "Node.js install done"
    }
}
Log ""

# ── 5. PostgreSQL + pgvector (optional) ──────────────────────────────────────
if ($InstallPostgres -eq "1") {
    Log "--- Step 5: PostgreSQL 16 ---"
    Log "TIP: PostgreSQL is needed for the SOUL long-term memory feature."

    $pgService = Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pgService) {
        Log "SKIP: PostgreSQL service '$($pgService.Name)' already exists"
    } else {
        $rc = Invoke-Winget @("install","-e","--id","PostgreSQL.PostgreSQL.16","--silent","--accept-package-agreements","--accept-source-agreements")
        if ($rc -ne 0) {
            $pgBuild = "16.9-1"
            $pgUrl   = "https://get.enterprisedb.com/postgresql/postgresql-$pgBuild-windows-x64.exe"
            $pgDest  = Join-Path $env:TEMP "postgresql-$pgBuild-windows-x64.exe"
            Log "Downloading PostgreSQL $pgBuild from EDB..."
            try {
                (New-Object System.Net.WebClient).DownloadFile($pgUrl, $pgDest)
                $p = Start-Process $pgDest -ArgumentList `
                    "--mode unattended --unattendedmodeui none --superpassword postgres --servicename postgresql-16 --serverport 5432" `
                    -Wait -PassThru
                Log "PostgreSQL installed (exit $($p.ExitCode))"
            } catch {
                Log "ERROR: PostgreSQL download failed: $_"
            }
        } else {
            Log "PostgreSQL install done"
        }
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path","User")
    }
    Log ""

    # ── pgvector ──────────────────────────────────────────────────────────────
    Log "--- Step 5b: pgvector extension ---"
    Log "TIP: pgvector adds vector search to PostgreSQL, required for SOUL memory."

    # Find PostgreSQL lib and extension dirs
    $pgPaths = @(
        "C:\Program Files\PostgreSQL\16",
        "C:\Program Files\PostgreSQL\15"
    )
    $pgBase = $pgPaths | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $pgBase) {
        # Try registry
        $pgReg = Get-ItemProperty "HKLM:\SOFTWARE\PostgreSQL\Installations\postgresql-x64-16" -ErrorAction SilentlyContinue
        if ($pgReg) { $pgBase = $pgReg.Base }
    }

    if (-not $pgBase) {
        Log "WARN: PostgreSQL install directory not found — install pgvector manually:"
        Log "      https://github.com/pgvector/pgvector#windows"
    } else {
        $pgLib  = Join-Path $pgBase "lib"
        $pgExt  = Join-Path $pgBase "share\extension"
        $pgBin  = Join-Path $pgBase "bin"

        # Download latest pgvector release for pg16 windows
        $pvTag    = "v0.8.0"
        $pvZipUrl = "https://github.com/pgvector/pgvector/releases/download/$pvTag/pgvector-$pvTag-pg16-windows-x86_64.zip"
        $pvZip    = Join-Path $env:TEMP "pgvector-pg16-windows.zip"
        $pvExtract= Join-Path $env:TEMP "pgvector-extract"

        Log "Downloading pgvector $pvTag for PostgreSQL 16..."
        try {
            (New-Object System.Net.WebClient).DownloadFile($pvZipUrl, $pvZip)
            Remove-Item $pvExtract -Recurse -Force -ErrorAction SilentlyContinue
            Expand-Archive -Path $pvZip -DestinationPath $pvExtract -Force

            # Copy lib/*.dll → pg lib dir
            Get-ChildItem "$pvExtract\lib\*.dll" -ErrorAction SilentlyContinue | ForEach-Object {
                Copy-Item $_.FullName -Destination $pgLib -Force
                Log "  Copied $($_.Name) -> $pgLib"
            }
            # Copy share/extension/* → pg extension dir
            Get-ChildItem "$pvExtract\share\extension\*" -ErrorAction SilentlyContinue | ForEach-Object {
                Copy-Item $_.FullName -Destination $pgExt -Force
                Log "  Copied $($_.Name) -> $pgExt"
            }
            Log "OK: pgvector files installed to $pgBase"
            Log "    Run in psql: CREATE EXTENSION vector;"
        } catch {
            Log "ERROR: pgvector download/install failed: $_"
            Log "       Install manually: https://github.com/pgvector/pgvector#windows"
        }
    }
    Log ""
}

Log "=== Prerequisites done ==="
Log "Log: $TempLog  (also $AppLog)"
Log ""
Write-Host "Press any key to close this window..." -ForegroundColor Cyan
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
