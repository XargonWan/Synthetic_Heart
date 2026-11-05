<#
.SYNOPSIS
    Build the SyntH Windows installer with Inno Setup.

.DESCRIPTION
    Compiles installer\synth-installer.iss into
    installer\Output\SyntH-Setup-<version>.exe.

    Inno Setup 6 must be installed. If pgvector has not been vendored under
    installer\vendor\pgvector\ the installer still builds, but semantic memory
    search will be off on machines it installs to; the script says so. Run the
    "pgvector for Windows" GitHub workflow to produce those files.

.PARAMETER SkipVendorWarning
    Do not warn about a missing installer\vendor\pgvector.

.EXAMPLE
    installer\build_installer.ps1
#>

[CmdletBinding()]
param(
    [string]$Version,
    [switch]$SkipVendorWarning
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$iss = Join-Path $PSScriptRoot 'synth-installer.iss'

function Resolve-Version {
    # The repository has no version file: GitVersion (GitVersion.yml) is the
    # source of truth and CI passes its output to this script. Locally, fall back
    # to the newest tag, then to a placeholder so a build always succeeds.
    param([string]$Explicit)

    if ($Explicit) { return $Explicit }

    $fromGitVersion = Get-Command dotnet-gitversion -ErrorAction SilentlyContinue
    if ($fromGitVersion) {
        try {
            $json = & dotnet-gitversion | ConvertFrom-Json
            if ($json.MajorMinorPatch) { return $json.MajorMinorPatch }
        } catch { }
    }

    try {
        $tag = (& git -C $repoRoot describe --tags --abbrev=0 2>$null)
        # A tag is not necessarily a version: this repository has one called "legacy", and
        # it is the newest. Passing it on made ISCC fail on VersionInfoVersion - "Value of
        # [Setup] section directive VersionInfoVersion is invalid" - with nothing pointing
        # at the tag as the cause. Only something version-shaped is accepted.
        if ($LASTEXITCODE -eq 0 -and $tag -match '^v?\d+(\.\d+){1,3}') {
            return ($tag -replace '^v', '')
        }
    } catch { }

    return '0.0.0-dev'
}

function Resolve-VersionInfoVersion {
    # Windows version resources are numeric x.y.z only, while a build version may
    # carry a suffix the file properties cannot: "1.0.0a" (a revision of 1.0.0)
    # or "1.2.3-feat.4" (a GitVersion pre-release). ISCC refuses anything else
    # with "Value of [Setup] section directive VersionInfoVersion is invalid",
    # which names the field and never the suffix, so the leading numeric run is
    # derived here and passed to the .iss explicitly. This script is the single
    # place that knows the rule.
    param([string]$AppVersion)

    if ($AppVersion -match '^\d+(\.\d+){1,3}') { return $Matches[0] }
    return '0.0.0'
}

$version = Resolve-Version -Explicit $Version
$versionInfoVersion = Resolve-VersionInfoVersion -AppVersion $version
$output = Join-Path $PSScriptRoot "Output\SyntH-Setup-$version.exe"

function Find-Iscc {
    $possible = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    )
    # The @() around the pipeline is load-bearing. Piping a list through Where-Object
    # unwraps a single match out of the array into a bare string, and indexing a string
    # gives its first character, so $candidates[0] was "C" and Inno Setup was never
    # actually run: the script printed "compiler: C" and died on
    # "The term 'C' is not recognized". One match is the ordinary case, because a
    # machine has one Inno Setup installed.
    $candidates = @($possible | Where-Object { $_ -and (Test-Path $_) })
    if ($candidates.Count -gt 0) { return $candidates[0] }
    return $null
}

$iscc = Find-Iscc
if (-not $iscc) {
    Write-Host 'Inno Setup 6 was not found.' -ForegroundColor Red
    Write-Host '  Install it from https://jrsoftware.org/isdl.php (or: winget install JRSoftware.InnoSetup)'
    Write-Host '  then run this script again.'
    exit 1
}

if (-not (Test-Path $iss)) { Write-Host "missing $iss" -ForegroundColor Red; exit 1 }

$vendor = Join-Path $PSScriptRoot 'vendor\pgvector'
if (-not (Test-Path $vendor) -and -not $SkipVendorWarning) {
    Write-Host 'note: installer\vendor\pgvector is absent.' -ForegroundColor Yellow
    Write-Host '      The installer will still build, but installed copies get no vector'
    Write-Host '      extension, so SOUL memory search runs in memory-only mode.'
    Write-Host '      Produce the files with the "pgvector for Windows" workflow.'
    Write-Host ''
}

Write-Host "Building SyntH $version with Inno Setup" -ForegroundColor Cyan
Write-Host "  compiler: $iscc"
Write-Host "  script:   $iss"
Write-Host ''

& $iscc "/DAppVersion=$version" "/DAppVersionNumeric=$versionInfoVersion" $iss
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ''
    Write-Host "Inno Setup failed with exit code $code" -ForegroundColor Red
    exit $code
}

if (Test-Path $output) {
    $size = [math]::Round((Get-Item $output).Length / 1MB, 1)
    Write-Host ''
    Write-Host "Built $output ($size MB)" -ForegroundColor Green
} else {
    Write-Host ''
    Write-Host "Inno Setup reported success but $output is missing" -ForegroundColor Yellow
    exit 1
}
