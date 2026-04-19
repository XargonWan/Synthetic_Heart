<#
.SYNOPSIS
    Repeatable build script for SyntH-Setup.exe.

.DESCRIPTION
    Run this script whenever a new SyntH version is released to produce a
    fresh installer.  All vendor dependencies are downloaded automatically
    if not already present.

    Steps:
      1. Read version from ../version.txt
      2. Ensure Inno Setup 6 compiler (iscc.exe) is present; install if missing
      3. Download MariaDB MSI to vendor/ (if absent or pinned version changed)
      4. Download NSSM to vendor/ (if absent)
      5. Clean installer/Output/
      6. Compile: iscc synth-installer.iss
      7. Report output file + size

.USAGE
    .\installer\build_installer.ps1

.NOTES
    Bump $MariaDbVersion when MariaDB releases a new minor that you want to ship.
    NSSM 2.24 is stable; no need to bump unless a new release appears.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# -- Version pins -------------------------------------------------------------
$MariaDbVersion   = "11.4.5"   # update when a new stable minor is released
$NssmVersion      = "2.24"
$InnoSetupVersion = "6.3.3"

# -- Paths --------------------------------------------------------------------
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot    = Split-Path -Parent $ScriptDir
$VendorDir   = Join-Path $ScriptDir "vendor"
$OutputDir   = Join-Path $ScriptDir "Output"
$IssFile     = Join-Path $ScriptDir "synth-installer.iss"
$VersionFile = Join-Path $RepoRoot "version.txt"

# -- Read version -------------------------------------------------------------
if (-not (Test-Path $VersionFile)) {
    Write-Error "version.txt not found at $VersionFile"
    exit 1
}
$AppVersion = (Get-Content $VersionFile -Raw).Trim()
Write-Host "Building SyntH-Setup-$AppVersion.exe" -ForegroundColor Cyan

# -- Ensure vendor dir --------------------------------------------------------
New-Item -ItemType Directory -Force -Path $VendorDir | Out-Null

# -- Helper: download a URL to a file, showing progress ----------------------
function Download-File {
    param(
        [string]$Url,
        [string]$Dest,
        [string]$Label
    )
    Write-Host "  Downloading $Label..." -NoNewline
    $wc = New-Object System.Net.WebClient
    $wc.DownloadFile($Url, $Dest)
    $sizeMB = [math]::Round((Get-Item $Dest).Length / 1MB, 1)
    Write-Host " $sizeMB MB" -ForegroundColor Green
}

# -- Helper: verify SHA-256 ---------------------------------------------------
function Verify-Hash {
    param([string]$File, [string]$Expected)
    $actual = (Get-FileHash $File -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $Expected.ToLower()) {
        Write-Error "Hash mismatch for $File`n  expected: $Expected`n  got:      $actual"
        exit 1
    }
    Write-Host "  Hash OK" -ForegroundColor Green
}

# -- Step 2: Inno Setup -------------------------------------------------------
$IsccCandidates = @(
    "C:\Program Files (x86)\Inno Setup 6\iscc.exe",
    "C:\Program Files\Inno Setup 6\iscc.exe"
)
$IsccPath = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $IsccPath) {
    Write-Host "Inno Setup not found - downloading and installing..." -ForegroundColor Yellow
    $InnoUrl  = "https://jrsoftware.org/download.php/is.exe"
    $InnoInst = Join-Path $env:TEMP "inno-setup-$InnoSetupVersion.exe"
    Download-File -Url $InnoUrl -Dest $InnoInst -Label "Inno Setup $InnoSetupVersion"
    Start-Process -FilePath $InnoInst -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" -Wait
    $IsccPath = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $IsccPath) {
        Write-Error "Inno Setup install failed or iscc.exe not found in expected location"
        exit 1
    }
}
Write-Host "  iscc.exe: $IsccPath" -ForegroundColor Green

# -- Step 3: MariaDB MSI ------------------------------------------------------
$MsiPattern = "mariadb-$MariaDbVersion-winx64.msi"
$MsiDest    = Join-Path $VendorDir $MsiPattern
$MsiUrl     = "https://downloads.mariadb.org/rest-api/mariadb/$MariaDbVersion/mariadb-$MariaDbVersion-winx64.msi"

# Remove MSIs from old versions
Get-ChildItem -Path $VendorDir -Filter "mariadb-*.msi" | Where-Object { $_.Name -ne $MsiPattern } | Remove-Item -Force

if (-not (Test-Path $MsiDest)) {
    Write-Host "Fetching MariaDB $MariaDbVersion MSI URL from downloads API..."
    # Resolve actual download URL from the MariaDB downloads REST API
    try {
        $ApiUrl  = "https://downloads.mariadb.org/rest-api/mariadb/$MariaDbVersion/"
        $Resp    = Invoke-RestMethod -Uri $ApiUrl -ErrorAction Stop
        $Release = $Resp.releases.PSObject.Properties.Value | Select-Object -First 1
        $MsiFile = $Release.files | Where-Object { $_.package_type -eq "MSI Package" -and $_.os -eq "Windows" } | Select-Object -First 1
        if ($MsiFile) {
            $MsiUrl = $MsiFile.url
            Write-Host "  Resolved MSI URL: $MsiUrl"
        }
    } catch {
        Write-Host "  (Could not resolve from API, using default URL)" -ForegroundColor Yellow
    }
    Download-File -Url $MsiUrl -Dest $MsiDest -Label "MariaDB $MariaDbVersion MSI"
} else {
    Write-Host "  MariaDB $MariaDbVersion MSI already cached" -ForegroundColor Green
}

# -- Step 4: NSSM -------------------------------------------------------------
$NssmDest = Join-Path $VendorDir "nssm.exe"
if (-not (Test-Path $NssmDest)) {
    $NssmZipUrl  = "https://nssm.cc/release/nssm-$NssmVersion.zip"
    $NssmZipDest = Join-Path $env:TEMP "nssm-$NssmVersion.zip"
    Download-File -Url $NssmZipUrl -Dest $NssmZipDest -Label "NSSM $NssmVersion"
    $ExtractTo = Join-Path $env:TEMP "nssm-extract"
    Expand-Archive -Path $NssmZipDest -DestinationPath $ExtractTo -Force
    $NssmBin = Get-ChildItem -Path $ExtractTo -Recurse -Filter "nssm.exe" |
               Where-Object { $_.DirectoryName -match "win64" } |
               Select-Object -First 1
    if (-not $NssmBin) {
        $NssmBin = Get-ChildItem -Path $ExtractTo -Recurse -Filter "nssm.exe" | Select-Object -First 1
    }
    Copy-Item -Path $NssmBin.FullName -Destination $NssmDest -Force
    Write-Host "  NSSM extracted" -ForegroundColor Green
} else {
    Write-Host "  NSSM already cached" -ForegroundColor Green
}

# -- Step 5: Clean output -----------------------------------------------------
if (Test-Path $OutputDir) {
    Remove-Item -Path (Join-Path $OutputDir "*") -Recurse -Force
    Write-Host "  Output/ cleaned" -ForegroundColor Green
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

# -- Step 6: Compile ----------------------------------------------------------
Write-Host "`nCompiling installer..." -ForegroundColor Cyan
& $IsccPath $IssFile
if ($LASTEXITCODE -ne 0) {
    Write-Error "iscc.exe failed (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}

# -- Step 7: Report -----------------------------------------------------------
$ExeName = "SyntH-Setup-$AppVersion.exe"
$ExePath = Join-Path $OutputDir $ExeName
if (Test-Path $ExePath) {
    $SizeMB = [math]::Round((Get-Item $ExePath).Length / 1MB, 1)
    Write-Host "`n  Output: $ExePath  ($SizeMB MB)" -ForegroundColor Green
    Write-Host "  Done. Distribute this file to end users." -ForegroundColor Green
} else {
    # iscc may use a slightly different name - just find the .exe
    $Found = Get-ChildItem -Path $OutputDir -Filter "*.exe" | Select-Object -First 1
    if ($Found) {
        $SizeMB = [math]::Round($Found.Length / 1MB, 1)
        Write-Host "`n  Output: $($Found.FullName)  ($SizeMB MB)" -ForegroundColor Green
    } else {
        Write-Warning "No .exe found in $OutputDir - check iscc output above"
    }
}
