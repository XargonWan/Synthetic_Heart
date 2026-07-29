from __future__ import annotations

import asyncio
import gzip
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.db import (
    _get_db_type,
    _get_source_db_type,
    _read_db_config,
    _read_source_db_config,
)
from core.logging_utils import log_error, log_info, log_warning


@dataclass(slots=True)
class DatabaseBackupPlan:
    backend: str
    database: str
    output_path: Path
    command: tuple[str, ...]
    env: dict[str, str]


_backup_task: asyncio.Task[None] | None = None


def _backups_dir() -> Path:
    backups_dir = Path(os.environ.get("SYNTH_BACKUPS_DIR", "backups")).expanduser()
    backups_dir.mkdir(parents=True, exist_ok=True)
    return backups_dir


def _backup_interval_seconds() -> float:
    raw_value = os.environ.get("SYNTH_DB_BACKUP_INTERVAL_HOURS", "24")
    try:
        hours = float(raw_value)
    except (TypeError, ValueError):
        hours = 24.0
    return max(1.0, hours * 3600.0)


def backups_enabled() -> bool:
    value = str(os.environ.get("SYNTH_DB_BACKUP_ENABLED", "1") or "1").strip()
    return value.lower() not in {"0", "false", "no", "off"}


def build_database_backup_plan(
    *,
    now: datetime | None = None,
    filename_prefix: str | None = None,
) -> DatabaseBackupPlan:
    backend = _get_db_type()
    host, port, user, password, database = _read_db_config()
    return build_database_backup_plan_for_connection(
        backend=backend,
        host=str(host),
        port=int(port),
        user=str(user),
        password=str(password),
        database=str(database),
        filename_prefix=filename_prefix or f"runtime-{backend}",
        now=now,
    )


def build_source_database_backup_plan(
    *,
    now: datetime | None = None,
    filename_prefix: str | None = None,
) -> DatabaseBackupPlan:
    backend = _get_source_db_type()
    host, port, user, password, database = _read_source_db_config()
    return build_database_backup_plan_for_connection(
        backend=backend,
        host=str(host),
        port=int(port),
        user=str(user),
        password=str(password),
        database=str(database),
        filename_prefix=filename_prefix or "legacy-source",
        now=now,
    )


def build_database_backup_plan_for_connection(
    *,
    backend: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    filename_prefix: str,
    now: datetime | None = None,
) -> DatabaseBackupPlan:
    backup_time = now or datetime.now(timezone.utc)
    timestamp = backup_time.strftime("%Y%m%dT%H%M%SZ")
    backup_root = _backups_dir()
    env = os.environ.copy()

    if backend == "postgres":
        env["PGPASSWORD"] = str(password)
        return DatabaseBackupPlan(
            backend=backend,
            database=str(database),
            output_path=backup_root
            / f"{filename_prefix}-{database}-{timestamp}.sql.gz",
            command=(
                shutil.which("pg_dump") or "pg_dump",
                "--host",
                host,
                "--port",
                str(port),
                "--username",
                user,
                "--dbname",
                database,
                "--format=plain",
                "--no-owner",
                "--no-privileges",
                "--encoding=UTF8",
            ),
            env=env,
        )

    env["MYSQL_PWD"] = str(password)
    return DatabaseBackupPlan(
        backend=backend,
        database=str(database),
        output_path=backup_root / f"{filename_prefix}-{database}-{timestamp}.sql.gz",
        command=(
            shutil.which("mysqldump") or "mysqldump",
            "--host",
            host,
            "--port",
            str(port),
            "--user",
            user,
            "--single-transaction",
            "--quick",
            "--routines",
            "--triggers",
            "--skip-lock-tables",
            database,
        ),
        env=env,
    )


_TABLE_NAME_RE = None  # lazily compiled in _sanitize_table_names


def _sanitize_table_names(tables: list[str]) -> list[str]:
    """Validate and de-duplicate table identifiers for a per-table backup.

    Only plain SQL identifiers are allowed (letters, digits, underscore,
    optionally a single ``schema.table`` qualifier). Anything else is
    rejected to keep the value out of the shelled-out dump command.
    """
    global _TABLE_NAME_RE
    if _TABLE_NAME_RE is None:
        _TABLE_NAME_RE = re.compile(
            r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$"
        )

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tables:
        name = str(raw).strip()
        if not name:
            continue
        if not _TABLE_NAME_RE.match(name):
            raise ValueError(f"Invalid table identifier: {raw!r}")
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if not cleaned:
        raise ValueError("At least one valid table name is required")
    return cleaned


def build_database_backup_plan_for_tables(
    tables: list[str],
    *,
    now: datetime | None = None,
    filename_prefix: str | None = None,
) -> DatabaseBackupPlan:
    """Build a backup plan restricted to *tables* for the runtime database."""
    backend = _get_db_type()
    host, port, user, password, database = _read_db_config()
    safe_tables = _sanitize_table_names(list(tables))

    backup_time = now or datetime.now(timezone.utc)
    timestamp = backup_time.strftime("%Y%m%dT%H%M%SZ")
    backup_root = _backups_dir()
    env = os.environ.copy()
    prefix = filename_prefix or f"runtime-{backend}-tables"

    if backend == "postgres":
        env["PGPASSWORD"] = str(password)
        table_args: list[str] = []
        for tbl in safe_tables:
            table_args.extend(("--table", tbl))
        return DatabaseBackupPlan(
            backend=backend,
            database=str(database),
            output_path=backup_root / f"{prefix}-{database}-{timestamp}.sql.gz",
            command=(
                shutil.which("pg_dump") or "pg_dump",
                "--host",
                str(host),
                "--port",
                str(port),
                "--username",
                str(user),
                "--dbname",
                str(database),
                "--format=plain",
                "--no-owner",
                "--no-privileges",
                "--encoding=UTF8",
                *table_args,
            ),
            env=env,
        )

    env["MYSQL_PWD"] = str(password)
    return DatabaseBackupPlan(
        backend=backend,
        database=str(database),
        output_path=backup_root / f"{prefix}-{database}-{timestamp}.sql.gz",
        command=(
            shutil.which("mysqldump") or "mysqldump",
            "--host",
            str(host),
            "--port",
            str(port),
            "--user",
            str(user),
            "--single-transaction",
            "--quick",
            "--routines",
            "--triggers",
            "--skip-lock-tables",
            str(database),
            *safe_tables,
        ),
        env=env,
    )


async def create_table_backup(
    tables: list[str],
    *,
    reason: str = "manual_table_webui",
    filename_prefix: str | None = None,
) -> Path | None:
    """Create a gzip SQL dump restricted to *tables* for the runtime DB."""
    plan = build_database_backup_plan_for_tables(
        tables, filename_prefix=filename_prefix
    )
    return await create_backup_from_plan(plan, reason=reason)


def _run_backup_sync(plan: DatabaseBackupPlan) -> None:
    executable = str(plan.command[0])
    if not Path(executable).exists() and shutil.which(executable) is None:
        raise RuntimeError(f"Backup executable not found: {executable}")

    with subprocess.Popen(
        plan.command,
        env=plan.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        stdout_pipe = process.stdout
        assert stdout_pipe is not None
        with gzip.open(plan.output_path, "wb") as handle:
            for chunk in iter(lambda: stdout_pipe.read(1024 * 1024), b""):
                handle.write(chunk)
        _, stderr_bytes = process.communicate()
        return_code = process.returncode

    if return_code == 0:
        return

    try:
        plan.output_path.unlink(missing_ok=True)
    except Exception:
        pass

    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    raise RuntimeError(
        f"{plan.backend} backup failed with exit code {return_code}: {stderr}"
    )


async def create_database_backup(
    *,
    reason: str = "scheduled",
    force: bool = False,
    filename_prefix: str | None = None,
) -> Path | None:
    if not force and not backups_enabled():
        log_info("[db_backup] Backup scheduler disabled; skipping backup request")
        return None

    plan = build_database_backup_plan(filename_prefix=filename_prefix)
    return await create_backup_from_plan(plan, reason=reason)


async def create_source_database_backup(
    *,
    reason: str = "pre_migration",
    filename_prefix: str | None = None,
) -> Path | None:
    plan = build_source_database_backup_plan(filename_prefix=filename_prefix)
    return await create_backup_from_plan(plan, reason=reason)


async def create_backup_from_plan(
    plan: DatabaseBackupPlan, *, reason: str = "manual"
) -> Path | None:
    log_info(
        f"[db_backup] Starting {reason} {plan.backend} backup to {plan.output_path.name}"
    )
    try:
        await asyncio.to_thread(_run_backup_sync, plan)
    except Exception as exc:
        log_error(f"[db_backup] Backup failed: {exc}")
        return None

    log_info(f"[db_backup] Backup completed: {plan.output_path}")
    return plan.output_path


async def _backup_loop() -> None:
    interval_seconds = _backup_interval_seconds()
    log_info(
        f"[db_backup] Embedded database backup scheduler active every {interval_seconds / 3600.0:.2f}h"
    )
    while True:
        await asyncio.sleep(interval_seconds)
        await create_database_backup(reason="scheduled")


def start_database_backup_scheduler() -> asyncio.Task[None] | None:
    global _backup_task
    if not backups_enabled():
        log_info("[db_backup] Embedded database backups disabled")
        return None
    if _backup_task is not None and not _backup_task.done():
        return _backup_task

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log_warning(
            "[db_backup] No running event loop available; backup scheduler not started"
        )
        return None

    _backup_task = loop.create_task(_backup_loop())
    return _backup_task
