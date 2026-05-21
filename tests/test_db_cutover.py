from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import db_cutover


@pytest.mark.asyncio
async def test_cutover_skips_when_runtime_is_not_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db_cutover, "_get_db_type", lambda: "mariadb")

    result = await db_cutover.maybe_run_legacy_mysql_cutover()

    assert result is False


@pytest.mark.asyncio
async def test_cutover_runs_backup_and_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "db-cutover-state.json"
    monkeypatch.setenv("SYNTH_DB_CUTOVER_STATE_PATH", str(state_path))
    monkeypatch.setattr(db_cutover, "_get_db_type", lambda: "postgres")
    monkeypatch.setattr(db_cutover, "_get_source_db_type", lambda: "mariadb")
    monkeypatch.setattr(
        db_cutover,
        "migrate_legacy_soul_postgres_if_needed",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        db_cutover, "_legacy_source_has_data", AsyncMock(return_value=True)
    )
    runtime_backup = AsyncMock(
        return_value=tmp_path / "premigration-runtime-postgres-synth.sql.gz"
    )
    monkeypatch.setattr(db_cutover, "create_database_backup", runtime_backup)
    source_backup = AsyncMock(
        return_value=tmp_path / "premigration-legacy-source-mariadb-synth.sql.gz"
    )
    monkeypatch.setattr(
        db_cutover,
        "create_source_database_backup",
        source_backup,
    )
    monkeypatch.setattr(
        db_cutover,
        "build_default_migration_config",
        lambda: SimpleNamespace(dry_run=True, audit_only=True),
    )

    fake_results: list[object] = [
        SimpleNamespace(name="config", migrated_rows=3, skipped=False)
    ]

    class FakeMigrator:
        def __init__(self, config: object) -> None:
            self.config = config

        async def run(self) -> list[object]:
            return fake_results

    monkeypatch.setattr(db_cutover, "MainDbMigrator", FakeMigrator)

    result = await db_cutover.maybe_run_legacy_mysql_cutover()

    assert result is True
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["legacy_status"] == "completed"
    assert payload["runtime_backup_path"].endswith(
        "premigration-runtime-postgres-synth.sql.gz"
    )
    assert payload["migrated_rows"] == 3
    assert payload["legacy_backup_path"].endswith(
        "premigration-legacy-source-mariadb-synth.sql.gz"
    )
    assert payload["tables"] == {"config": 3}
    runtime_backup.assert_awaited_once_with(
        reason="pre_migration",
        force=True,
        filename_prefix="premigration-runtime-postgres",
    )
    source_backup.assert_awaited_once_with(
        reason="pre_migration",
        filename_prefix="premigration-legacy-source-mariadb",
    )


def test_legacy_soul_source_dsn_uses_dedicated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        db_cutover,
        "build_runtime_postgres_dsn",
        lambda: "postgresql://synth:synth@synth-db:5432/synth",
    )
    monkeypatch.setenv(
        "LEGACY_SOUL_POSTGRES_DSN",
        "postgresql://soul:soul@synth-soul-db:5432/soul_memory",
    )
    monkeypatch.delenv("SOUL_POSTGRES_DSN", raising=False)

    assert db_cutover._legacy_soul_source_dsn() == (
        "postgresql://soul:soul@synth-soul-db:5432/soul_memory"
    )


def test_legacy_soul_source_dsn_falls_back_to_old_soul_env_when_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        db_cutover,
        "build_runtime_postgres_dsn",
        lambda: "postgresql://synth:synth@synth-db:5432/synth",
    )
    monkeypatch.delenv("LEGACY_SOUL_POSTGRES_DSN", raising=False)
    monkeypatch.setenv(
        "SOUL_POSTGRES_DSN",
        "postgresql://soul:soul@synth-soul-db:5432/soul_memory",
    )

    assert db_cutover._legacy_soul_source_dsn() == (
        "postgresql://soul:soul@synth-soul-db:5432/soul_memory"
    )


@pytest.mark.asyncio
async def test_soul_cutover_updates_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "db-cutover-state.json"
    monkeypatch.setenv("SYNTH_DB_CUTOVER_STATE_PATH", str(state_path))
    monkeypatch.setattr(db_cutover, "_get_db_type", lambda: "postgres")
    monkeypatch.setattr(
        db_cutover,
        "build_runtime_postgres_dsn",
        lambda: "postgresql://synth:synth@synth-db:5432/synth",
    )
    monkeypatch.setattr(
        db_cutover,
        "_legacy_soul_source_dsn",
        lambda: "postgresql://soul:soul@synth-soul-db:5432/soul_memory",
    )
    monkeypatch.setattr(
        db_cutover,
        "_legacy_soul_source_has_data",
        AsyncMock(return_value=True),
    )
    runtime_backup = AsyncMock(
        return_value=tmp_path / "premigration-runtime-postgres-synth.sql.gz"
    )
    monkeypatch.setattr(db_cutover, "create_database_backup", runtime_backup)
    soul_backup = AsyncMock(
        return_value=(tmp_path / "premigration-legacy-soul-postgres-soul_memory.sql.gz")
    )
    monkeypatch.setattr(
        db_cutover,
        "create_backup_from_plan",
        soul_backup,
    )
    monkeypatch.setattr(
        db_cutover,
        "_migrate_legacy_soul_tables",
        AsyncMock(return_value={"mem_cells": 2, "dsp_versions": 1}),
    )

    state = db_cutover.DbCutoverState()

    result = await db_cutover.migrate_legacy_soul_postgres_if_needed(state)

    assert result is True
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["soul_status"] == "completed"
    assert payload["runtime_backup_path"].endswith(
        "premigration-runtime-postgres-synth.sql.gz"
    )
    assert payload["legacy_soul_backup_path"].endswith(
        "premigration-legacy-soul-postgres-soul_memory.sql.gz"
    )
    assert payload["soul_migrated_rows"] == 3
    assert payload["soul_tables"] == {"mem_cells": 2, "dsp_versions": 1}
    runtime_backup.assert_awaited_once_with(
        reason="pre_migration",
        force=True,
        filename_prefix="premigration-runtime-postgres",
    )
    soul_backup.assert_awaited_once()
    await_args = soul_backup.await_args
    assert await_args is not None
    plan = await_args.args[0]
    assert plan.output_path.name.startswith(
        "premigration-legacy-soul-postgres-soul_memory-"
    )
    assert await_args.kwargs["reason"] == "pre_soul_migration"
