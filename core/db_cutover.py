from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from core.db import (
    _get_db_type,
    _get_source_db_type,
    build_runtime_postgres_dsn,
    connect_postgres_dsn,
    connect_source_db,
)
from core.db_backup import (
    DatabaseBackupPlan,
    build_database_backup_plan_for_connection,
    create_database_backup,
    create_backup_from_plan,
    create_source_database_backup,
)
from core.logging_utils import log_error, log_info, log_warning
from core.main_db_migration import MainDbMigrator, build_default_migration_config
from core.soul.repository import PostgresSoulRepository


@dataclass(slots=True)
class DbCutoverState:
    status: str = "pending"
    soul_status: str = "pending"
    legacy_status: str = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    runtime_backup_path: str | None = None
    legacy_backup_path: str | None = None
    legacy_soul_backup_path: str | None = None
    migrated_rows: int = 0
    tables: dict[str, int] = field(default_factory=dict)
    soul_migrated_rows: int = 0
    soul_tables: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None


@dataclass(slots=True)
class _SoulTableSpec:
    name: str
    columns: tuple[str, ...]
    conflict_keys: tuple[str, ...]
    json_columns: tuple[str, ...] = ()
    source_query: str | None = None
    sequence_name: str | None = None


_SOUL_TABLE_SPECS: tuple[_SoulTableSpec, ...] = (
    _SoulTableSpec(
        name="mem_cells",
        columns=(
            "id",
            "session_id",
            "episodic_trace",
            "atomic_facts",
            "emotional_tag",
            "foresight_signals",
            "event_timestamp",
            "retrieval_count",
            "explicit_importance",
            "consolidated",
            "scene_id",
            "created_at",
            "updated_at",
        ),
        conflict_keys=("id",),
        json_columns=("atomic_facts", "emotional_tag", "foresight_signals"),
    ),
    _SoulTableSpec(
        name="mem_cell_vectors",
        columns=("mem_cell_id", "embedding"),
        conflict_keys=("mem_cell_id",),
        source_query="SELECT mem_cell_id, embedding::text AS embedding FROM mem_cell_vectors",
    ),
    _SoulTableSpec(
        name="mem_scenes",
        columns=("id", "title", "summary", "cell_ids", "created_at", "updated_at"),
        conflict_keys=("id",),
        json_columns=("cell_ids",),
    ),
    _SoulTableSpec(
        name="kg_triples",
        columns=(
            "id",
            "subject",
            "predicate",
            "object",
            "valid_from",
            "valid_until",
            "scene_id",
        ),
        conflict_keys=("id",),
        sequence_name="kg_triples_id_seq",
    ),
    _SoulTableSpec(
        name="foresight_signals",
        columns=(
            "id",
            "content",
            "valid_until",
            "trigger",
            "emotional_implication",
            "source_cell_id",
            "priority",
            "archived",
            "created_at",
            "updated_at",
        ),
        conflict_keys=("id",),
        json_columns=("emotional_implication",),
        sequence_name="foresight_signals_id_seq",
    ),
    _SoulTableSpec(
        name="dsp_extractions",
        columns=(
            "id",
            "session_id",
            "extracted_at",
            "user_facts",
            "user_preferences",
            "ai_self_facts",
        ),
        conflict_keys=("id",),
        json_columns=("user_facts", "user_preferences", "ai_self_facts"),
    ),
    _SoulTableSpec(
        name="dsp_versions",
        columns=("id", "content", "created_at", "archived_at", "active"),
        conflict_keys=("id",),
    ),
    _SoulTableSpec(
        name="soul_emotion_snapshots",
        columns=(
            "id",
            "joy",
            "fear",
            "sad",
            "anger",
            "source",
            "context",
            "created_at",
        ),
        conflict_keys=("id",),
        sequence_name="soul_emotion_snapshots_id_seq",
    ),
    _SoulTableSpec(
        name="soul_metrics",
        columns=("metric_key", "metric_value", "measured_at"),
        conflict_keys=("metric_key", "measured_at"),
    ),
)


def _state_path() -> Path:
    raw_path = os.environ.get(
        "SYNTH_DB_CUTOVER_STATE_PATH", "/config/db-cutover-state.json"
    )
    path = Path(raw_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_state() -> DbCutoverState:
    path = _state_path()
    if not path.exists():
        return DbCutoverState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return DbCutoverState(status="pending")
    return DbCutoverState(**{**asdict(DbCutoverState()), **payload})


def _save_state(state: DbCutoverState) -> None:
    _state_path().write_text(
        json.dumps(asdict(state), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def auto_migration_enabled() -> bool:
    value = str(os.environ.get("SYNTH_AUTO_MIGRATE_LEGACY_DB", "1") or "1").strip()
    return value.lower() not in {"0", "false", "no", "off"}


def _normalize_postgres_dsn(dsn: str) -> str:
    parsed = urlparse(dsn)
    username = unquote(parsed.username) if parsed.username else ""
    password = unquote(parsed.password) if parsed.password else ""
    hostname = parsed.hostname or ""
    port = parsed.port or 5432
    database = parsed.path.lstrip("/")
    return f"postgresql://{username}:{password}@{hostname}:{port}/{database}"


def _legacy_soul_source_schema() -> str:
    return (
        str(
            os.environ.get("LEGACY_SOUL_POSTGRES_SCHEMA")
            or os.environ.get("SOUL_POSTGRES_SCHEMA")
            or "public"
        ).strip()
        or "public"
    )


def _legacy_soul_source_dsn() -> str:
    runtime_dsn = build_runtime_postgres_dsn().strip()
    candidates = [
        os.environ.get("LEGACY_SOUL_POSTGRES_DSN"),
        os.environ.get("LEGACY_SOUL_DSN"),
        os.environ.get("SOUL_POSTGRES_DSN"),
    ]
    normalized_runtime = _normalize_postgres_dsn(runtime_dsn) if runtime_dsn else ""
    for candidate in candidates:
        raw_candidate = str(candidate or "").strip()
        if not raw_candidate:
            continue
        normalized_candidate = _normalize_postgres_dsn(raw_candidate)
        if normalized_candidate == normalized_runtime:
            continue
        return raw_candidate
    return ""


async def _legacy_soul_source_has_data(dsn: str, *, schema: str) -> bool:
    if not dsn:
        return False

    source_conn = None
    try:
        source_conn = await connect_postgres_dsn(dsn)
        qualified_table = f'"{schema}"."mem_cells"'
        regclass = await source_conn.fetchval(
            f"SELECT to_regclass('{schema}.mem_cells')"
        )
        if regclass is None:
            return False
        exists = await source_conn.fetchval(
            f"SELECT EXISTS(SELECT 1 FROM {qualified_table} LIMIT 1)"
        )
        return bool(exists)
    except Exception as exc:
        log_info(f"[db_cutover] Legacy SOUL source not reachable: {exc}")
        return False
    finally:
        if source_conn is not None:
            try:
                await source_conn.close()
            except Exception:
                pass


def _build_legacy_soul_backup_plan(dsn: str) -> DatabaseBackupPlan:
    parsed = urlparse(dsn)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    user = unquote(parsed.username) if parsed.username else "synth"
    password = unquote(parsed.password) if parsed.password else ""
    database = parsed.path.lstrip("/") or "postgres"
    return build_database_backup_plan_for_connection(
        backend="postgres",
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        filename_prefix="premigration-legacy-soul-postgres",
    )


async def _ensure_runtime_premigration_backup(state: DbCutoverState) -> None:
    if state.runtime_backup_path:
        return

    backup_path = await create_database_backup(
        reason="pre_migration",
        force=True,
        filename_prefix=f"premigration-runtime-{_get_db_type()}",
    )
    if backup_path is None:
        raise RuntimeError("Pre-migration backup for runtime database failed")

    state.runtime_backup_path = str(backup_path)
    _save_state(state)


def _prepare_soul_value(
    column: str, value: object, *, json_columns: tuple[str, ...]
) -> object:
    if value is None:
        return None
    if column in json_columns:
        return json.dumps(value)
    return value


def _build_soul_upsert_sql(spec: _SoulTableSpec) -> str:
    placeholders: list[str] = []
    for index, column in enumerate(spec.columns, start=1):
        suffix = (
            "::jsonb"
            if column in spec.json_columns
            else "::vector"
            if column == "embedding"
            else ""
        )
        placeholders.append(f"${index}{suffix}")
    updates = ", ".join(
        f'"{column}" = EXCLUDED."{column}"'
        for column in spec.columns
        if column not in spec.conflict_keys
    )
    if not updates:
        updates = f'"{spec.conflict_keys[0]}" = EXCLUDED."{spec.conflict_keys[0]}"'
    quoted_columns = ", ".join(f'"{column}"' for column in spec.columns)
    conflict_keys = ", ".join(f'"{column}"' for column in spec.conflict_keys)
    return (
        f'INSERT INTO "{spec.name}" ({quoted_columns}) '
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT ({conflict_keys}) DO UPDATE SET {updates}"
    )


async def _set_target_sequence_if_needed(
    target_conn: Any, spec: _SoulTableSpec
) -> None:
    if not spec.sequence_name:
        return
    await target_conn.execute(
        f"SELECT setval('{spec.sequence_name}', COALESCE((SELECT MAX(id) FROM \"{spec.name}\"), 1), TRUE)"
    )


async def _migrate_legacy_soul_tables(
    *, source_dsn: str, source_schema: str, target_dsn: str
) -> dict[str, int]:
    source_conn = await connect_postgres_dsn(source_dsn)
    target_repo = PostgresSoulRepository(dsn=target_dsn)
    target_pool = await target_repo._get_pool()
    target_conn = await connect_postgres_dsn(target_dsn)
    migrated: dict[str, int] = {}
    try:
        async with target_pool.acquire() as pooled_target_conn:
            for spec in _SOUL_TABLE_SPECS:
                regclass = await source_conn.fetchval(
                    f"SELECT to_regclass('{source_schema}.{spec.name}')"
                )
                if regclass is None:
                    migrated[spec.name] = 0
                    continue

                selected_columns = ", ".join(f'"{column}"' for column in spec.columns)
                source_query = spec.source_query or (
                    f'SELECT {selected_columns} FROM "{source_schema}"."{spec.name}"'
                )
                rows = await source_conn.fetch(source_query)
                if not rows:
                    migrated[spec.name] = 0
                    continue

                insert_sql = _build_soul_upsert_sql(spec)
                values = [
                    tuple(
                        _prepare_soul_value(
                            column, row[column], json_columns=spec.json_columns
                        )
                        for column in spec.columns
                    )
                    for row in rows
                ]
                await pooled_target_conn.executemany(insert_sql, values)
                await _set_target_sequence_if_needed(target_conn, spec)
                migrated[spec.name] = len(values)
    finally:
        try:
            await target_conn.close()
        except Exception:
            pass
        try:
            await target_repo.close()
        except Exception:
            pass
        try:
            await source_conn.close()
        except Exception:
            pass
    return migrated


async def migrate_legacy_soul_postgres_if_needed(state: DbCutoverState) -> bool:
    source_dsn = _legacy_soul_source_dsn()
    target_dsn = build_runtime_postgres_dsn().strip()
    source_schema = _legacy_soul_source_schema()

    if state.soul_status == "completed":
        return False
    if not source_dsn or not target_dsn:
        if state.soul_status == "pending":
            state.soul_status = "skipped"
            _save_state(state)
        return False
    if not await _legacy_soul_source_has_data(source_dsn, schema=source_schema):
        state.soul_status = "skipped"
        _save_state(state)
        return False

    state.soul_status = "in_progress"
    state.status = "in_progress"
    state.started_at = state.started_at or datetime.now(timezone.utc).isoformat()
    state.last_error = None
    _save_state(state)

    await _ensure_runtime_premigration_backup(state)

    if not state.legacy_soul_backup_path:
        backup_plan = _build_legacy_soul_backup_plan(source_dsn)
        backup_path = await create_backup_from_plan(
            backup_plan, reason="pre_soul_migration"
        )
        if backup_path is None:
            raise RuntimeError("Pre-migration backup for legacy SOUL database failed")
        state.legacy_soul_backup_path = str(backup_path)
        _save_state(state)

    migrated_tables = await _migrate_legacy_soul_tables(
        source_dsn=source_dsn,
        source_schema=source_schema,
        target_dsn=target_dsn,
    )
    state.soul_tables = {
        name: count for name, count in migrated_tables.items() if count
    }
    state.soul_migrated_rows = sum(migrated_tables.values())
    state.soul_status = "completed"
    _save_state(state)
    log_info(
        f"[db_cutover] Legacy SOUL cutover completed with {state.soul_migrated_rows} migrated rows"
    )
    return state.soul_migrated_rows > 0


async def _legacy_source_has_data() -> bool:
    source_conn = None
    try:
        source_conn = await connect_source_db()
    except Exception as exc:
        log_info(f"[db_cutover] Legacy source database not reachable: {exc}")
        return False

    try:
        async with source_conn.cursor() as cursor:
            for table_name in ("config", "chat_history_cache", "ai_diary", "memories"):
                await cursor.execute("SHOW TABLES LIKE %s", (table_name,))
                if not await cursor.fetchone():
                    continue
                await cursor.execute(f"SELECT 1 FROM `{table_name}` LIMIT 1")
                if await cursor.fetchone():
                    return True
    except Exception as exc:
        log_warning(f"[db_cutover] Failed to inspect legacy source DB: {exc}")
    finally:
        try:
            source_conn.close()
        except Exception:
            pass
    return False


async def maybe_run_legacy_mysql_cutover() -> bool:
    if not auto_migration_enabled():
        log_info("[db_cutover] Automatic legacy DB migration disabled")
        return False
    if _get_db_type() != "postgres":
        return False

    state = _load_state()
    migrated_any = False

    try:
        migrated_any = (
            await migrate_legacy_soul_postgres_if_needed(state) or migrated_any
        )

        if state.legacy_status == "completed":
            state.status = "completed"
            _save_state(state)
            return migrated_any

        if not await _legacy_source_has_data():
            if state.legacy_status == "pending":
                state.legacy_status = "skipped"
            if state.soul_status in {"completed", "skipped"}:
                state.status = "completed"
            _save_state(state)
            return migrated_any

        state.status = "in_progress"
        state.legacy_status = "in_progress"
        state.started_at = state.started_at or datetime.now(timezone.utc).isoformat()
        state.last_error = None
        _save_state(state)

        await _ensure_runtime_premigration_backup(state)

        if not state.legacy_backup_path:
            backup_path = await create_source_database_backup(
                reason="pre_migration",
                filename_prefix=(f"premigration-legacy-source-{_get_source_db_type()}"),
            )
            if backup_path is None:
                raise RuntimeError(
                    "Pre-migration backup for legacy source database failed"
                )
            state.legacy_backup_path = str(backup_path)
            _save_state(state)

        config = build_default_migration_config()
        config.dry_run = False
        config.audit_only = False
        results = await MainDbMigrator(config).run()

        state.status = "completed"
        state.legacy_status = "completed"
        state.finished_at = datetime.now(timezone.utc).isoformat()
        state.migrated_rows = sum(result.migrated_rows for result in results)
        state.tables = {
            result.name: result.migrated_rows
            for result in results
            if result.migrated_rows or result.skipped
        }
        _save_state(state)
        log_info(
            f"[db_cutover] Legacy MySQL cutover completed with {state.migrated_rows} migrated rows"
        )
        return True or migrated_any
    except Exception as exc:
        state.status = "failed"
        if state.legacy_status == "in_progress":
            state.legacy_status = "failed"
        if state.soul_status == "in_progress":
            state.soul_status = "failed"
        state.finished_at = datetime.now(timezone.utc).isoformat()
        state.last_error = str(exc)
        _save_state(state)
        log_error(f"[db_cutover] Legacy DB cutover failed: {exc}")
        raise


async def resume_legacy_mysql_cutover_if_needed() -> bool:
    state = _load_state()
    if state.status in {"in_progress", "failed"} or state.soul_status in {
        "in_progress",
        "failed",
    }:
        log_warning(
            f"[db_cutover] Resuming legacy cutover from previous state: {state.status}"
        )
    return await maybe_run_legacy_mysql_cutover()
