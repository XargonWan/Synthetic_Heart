#!/usr/bin/env bash
set -euo pipefail

# Bootstraps SOUL PostgreSQL schema into the pgvector service.
# Usage: bash scripts/bootstrap_soul_postgres.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEMA_FILE="${ROOT_DIR}/scripts/sql/soul_memory_postgres.sql"

SOUL_PG_SERVICE="${SOUL_PG_SERVICE:-synth-soul-db}"
SOUL_PG_DB="${SOUL_PG_DB:-soul_memory}"
SOUL_PG_USER="${SOUL_PG_USER:-soul}"

if [[ ! -f "${SCHEMA_FILE}" ]]; then
  echo "Schema file not found: ${SCHEMA_FILE}" >&2
  exit 1
fi

cd "${ROOT_DIR}"

echo "Starting ${SOUL_PG_SERVICE} service if needed..."
docker compose up -d "${SOUL_PG_SERVICE}"

echo "Waiting for PostgreSQL readiness..."
for _ in {1..30}; do
  if docker compose exec -T "${SOUL_PG_SERVICE}" pg_isready -U "${SOUL_PG_USER}" -d "${SOUL_PG_DB}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! docker compose exec -T "${SOUL_PG_SERVICE}" pg_isready -U "${SOUL_PG_USER}" -d "${SOUL_PG_DB}" >/dev/null 2>&1; then
  echo "PostgreSQL did not become ready in time." >&2
  exit 1
fi

echo "Applying SOUL schema from ${SCHEMA_FILE}..."
cat "${SCHEMA_FILE}" | docker compose exec -T "${SOUL_PG_SERVICE}" psql -v ON_ERROR_STOP=1 -U "${SOUL_PG_USER}" -d "${SOUL_PG_DB}"

echo "SOUL PostgreSQL bootstrap completed."
