#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from core.main_db_migration import MainDbMigrator, build_default_migration_config


def _load_repo_env_defaults() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate Synthetic Heart runtime tables from MariaDB to PostgreSQL"
    )
    parser.add_argument(
        "--tables",
        type=str,
        default="",
        help="Optional comma-separated subset of tables to migrate",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows fetched per batch from MariaDB",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect source rows and emit counts without writing to PostgreSQL",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Inspect source schema drift only; do not read row batches or write target data",
    )
    parser.add_argument(
        "--schema-file",
        type=Path,
        default=None,
        help="Optional path to the PostgreSQL schema SQL file",
    )
    parser.add_argument(
        "--target-dsn",
        type=str,
        default="",
        help="Override target PostgreSQL DSN",
    )
    parser.add_argument(
        "--include-emotion-diary",
        action="store_true",
        help="Include legacy emotion_diary rows; skipped by default so the new system can start fresh unless explicitly requested",
    )
    return parser.parse_args()


async def run_cli() -> int:
    args = parse_args()
    _load_repo_env_defaults()
    config = build_default_migration_config()
    config.batch_size = max(1, int(args.batch_size))
    config.dry_run = bool(args.dry_run)
    config.audit_only = bool(args.audit_only)
    config.include_legacy_emotion_diary = bool(args.include_emotion_diary)
    if args.schema_file is not None:
        config.schema_path = args.schema_file
    if args.target_dsn:
        config.target_dsn = args.target_dsn
    if args.tables:
        config.tables = tuple(
            table.strip() for table in args.tables.split(",") if table.strip()
        )

    migrator = MainDbMigrator(config)
    results = await migrator.run()

    total_rows = sum(result.migrated_rows for result in results)
    print("Main DB migration summary")
    print(f"  dry_run: {config.dry_run}")
    print(f"  audit_only: {config.audit_only}")
    print(f"  include_legacy_emotion_diary: {config.include_legacy_emotion_diary}")
    print(f"  schema_file: {config.schema_path}")
    print(f"  migrated_rows: {total_rows}")
    if not args.tables and not config.include_legacy_emotion_diary:
        print(
            "  note: legacy emotion_diary is skipped by default; pass --include-emotion-diary to migrate it explicitly"
        )
    for result in results:
        print(
            f"  - {result.name}: migrated={result.migrated_rows} skipped={result.skipped} warnings={len(result.warnings)}"
        )
        for warning in result.warnings:
            print(f"      warning: {warning}")
    return 0


def main() -> int:
    return asyncio.run(run_cli())


if __name__ == "__main__":
    raise SystemExit(main())
