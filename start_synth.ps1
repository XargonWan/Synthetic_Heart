# start_synth.ps1 - Simple helper to run Synthetic Heart natively on Windows
# Usage: Open PowerShell, optionally set environment variables, then run this script

param(
    [switch]$NoVenv
)

Write-Host "Starting Synthetic Heart (native Windows helper)..."

if (-not $NoVenv) {
    if (-not (Test-Path .\.venv)) {
        Write-Host "Creating virtual environment .venv..."
        python -m venv .venv
    }
    Write-Host "Activating .venv..."
    . .\.venv\Scripts\Activate.ps1
}

# Example: set env vars if not present
if (-not $env:DB_HOST) { $env:DB_HOST = '127.0.0.1' }
if (-not $env:DB_PORT) { $env:DB_PORT = '3306' }
if (-not $env:WEBVIEW_HOST) { $env:WEBVIEW_HOST = 'localhost' }
if (-not $env:WEBVIEW_PORT) { $env:WEBVIEW_PORT = '3000' }

Write-Host "DB_HOST=$($env:DB_HOST) DB_PORT=$($env:DB_PORT)"
Write-Host "Starting Python application... (press Ctrl+C to stop)"
python main.py
