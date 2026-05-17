from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from core import db_backup


def test_build_database_backup_plan_for_postgres(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SYNTH_BACKUPS_DIR", str(tmp_path))
    monkeypatch.setattr(db_backup, "_get_db_type", lambda: "postgres")
    monkeypatch.setattr(
        db_backup,
        "_read_db_config",
        lambda: ("pg-host", 5432, "synth", "secret", "synth"),
    )

    plan = db_backup.build_database_backup_plan(
        now=datetime(2026, 5, 11, 10, 0, 0, tzinfo=timezone.utc)
    )

    assert plan.backend == "postgres"
    assert (
        plan.output_path == tmp_path / "runtime-postgres-synth-20260511T100000Z.sql.gz"
    )
    assert plan.command[0].endswith("pg_dump")
    assert "--dbname" in plan.command
    assert plan.env["PGPASSWORD"] == "secret"


def test_build_database_backup_plan_for_mariadb(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SYNTH_BACKUPS_DIR", str(tmp_path))
    monkeypatch.setattr(db_backup, "_get_db_type", lambda: "mariadb")
    monkeypatch.setattr(
        db_backup,
        "_read_db_config",
        lambda: ("db-host", 3306, "synth", "secret", "synth"),
    )

    plan = db_backup.build_database_backup_plan(
        now=datetime(2026, 5, 11, 10, 0, 0, tzinfo=timezone.utc)
    )

    assert plan.backend == "mariadb"
    assert (
        plan.output_path == tmp_path / "runtime-mariadb-synth-20260511T100000Z.sql.gz"
    )
    assert plan.command[0].endswith("mysqldump")
    assert "--single-transaction" in plan.command
    assert plan.env["MYSQL_PWD"] == "secret"


def test_build_database_backup_plan_with_premigration_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SYNTH_BACKUPS_DIR", str(tmp_path))
    monkeypatch.setattr(db_backup, "_get_db_type", lambda: "postgres")
    monkeypatch.setattr(
        db_backup,
        "_read_db_config",
        lambda: ("pg-host", 5432, "synth", "secret", "synth"),
    )

    plan = db_backup.build_database_backup_plan(
        now=datetime(2026, 5, 11, 10, 0, 0, tzinfo=timezone.utc),
        filename_prefix="premigration-runtime-postgres",
    )

    assert plan.output_path == (
        tmp_path / "premigration-runtime-postgres-synth-20260511T100000Z.sql.gz"
    )


@pytest.mark.asyncio
async def test_create_database_backup_runs_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = db_backup.DatabaseBackupPlan(
        backend="postgres",
        database="synth",
        output_path=tmp_path / "runtime-postgres-synth-test.sql.gz",
        command=("pg_dump",),
        env={},
    )
    calls: list[db_backup.DatabaseBackupPlan] = []

    async def _fake_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        func(*args, **kwargs)
        return None

    def _fake_run_backup_sync(received_plan: db_backup.DatabaseBackupPlan) -> None:
        calls.append(received_plan)

    monkeypatch.setattr(
        db_backup,
        "build_database_backup_plan",
        lambda filename_prefix=None: plan,
    )
    monkeypatch.setattr(db_backup.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(db_backup, "_run_backup_sync", _fake_run_backup_sync)

    result = await db_backup.create_database_backup(reason="test")

    assert result == plan.output_path
    assert calls == [plan]


@pytest.mark.asyncio
async def test_create_database_backup_force_bypasses_disabled_scheduler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = db_backup.DatabaseBackupPlan(
        backend="postgres",
        database="synth",
        output_path=tmp_path / "runtime-postgres-force.sql.gz",
        command=("pg_dump",),
        env={},
    )
    calls: list[db_backup.DatabaseBackupPlan] = []

    async def _fake_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        func(*args, **kwargs)
        return None

    def _fake_run_backup_sync(received_plan: db_backup.DatabaseBackupPlan) -> None:
        calls.append(received_plan)

    monkeypatch.setattr(db_backup, "backups_enabled", lambda: False)
    monkeypatch.setattr(
        db_backup,
        "build_database_backup_plan",
        lambda filename_prefix=None: plan,
    )
    monkeypatch.setattr(db_backup.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(db_backup, "_run_backup_sync", _fake_run_backup_sync)

    result = await db_backup.create_database_backup(reason="manual", force=True)

    assert result == plan.output_path
    assert calls == [plan]
