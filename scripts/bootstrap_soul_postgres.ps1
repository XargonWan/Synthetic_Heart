param(
    [string]$Service = $env:SOUL_PG_SERVICE,
    [string]$Database = $env:SOUL_PG_DB,
    [string]$User = $env:SOUL_PG_USER
)

if (-not $Service) { $Service = "synth-soul-db" }
if (-not $Database) { $Database = "soul_memory" }
if (-not $User) { $User = "soul" }

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$schema = Join-Path $root "scripts/sql/soul_memory_postgres.sql"

if (-not (Test-Path $schema)) {
    throw "Schema file not found: $schema"
}

Push-Location $root
try {
    Write-Host "Starting $Service service if needed..."
    docker compose up -d $Service | Out-Null

    Write-Host "Waiting for PostgreSQL readiness..."
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        $null = docker compose exec -T $Service pg_isready -U $User -d $Database 2>$null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 2
    }

    if (-not $ready) {
        throw "PostgreSQL did not become ready in time."
    }

    Write-Host "Applying SOUL schema from $schema..."
    Get-Content $schema -Raw | docker compose exec -T $Service psql -v ON_ERROR_STOP=1 -U $User -d $Database
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to apply SOUL schema."
    }

    Write-Host "SOUL PostgreSQL bootstrap completed."
}
finally {
    Pop-Location
}
