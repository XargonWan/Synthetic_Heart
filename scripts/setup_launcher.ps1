# SyntH setup wizard launcher — compatible with PowerShell 5.1+

$ErrorActionPreference = "Continue"

# Force UTF-8 so Rich's unicode characters (checkmarks etc.) don't crash on cp1252 consoles
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

# Refresh PATH from machine + user env (picks up winget-installed Python/uv/Node)
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path", "User")

$AppDir  = Split-Path -Parent $PSScriptRoot
$LogDir  = Join-Path $AppDir "logs"
$LogFile = Join-Path $LogDir "setup.log"

New-Item -Force -ItemType Directory -Path $LogDir | Out-Null

Write-Host "SyntH Setup Wizard" -ForegroundColor Cyan
Write-Host "Log: $LogFile" -ForegroundColor DarkGray
Write-Host ""

$WizardScript = Join-Path $PSScriptRoot "windows_setup.py"

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCmd) {
    & uv run python $WizardScript 2>>$LogFile
} else {
    & python $WizardScript 2>>$LogFile
}

$exitCode = $LASTEXITCODE

Write-Host ""
if ($exitCode -ne 0) {
    Write-Host "Setup wizard FAILED (exit $exitCode)." -ForegroundColor Red
    Write-Host "Full log: $LogFile" -ForegroundColor Yellow
} else {
    Write-Host "Setup complete." -ForegroundColor Green
    Write-Host "Log: $LogFile" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Press any key to close this window..." -ForegroundColor Cyan
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
