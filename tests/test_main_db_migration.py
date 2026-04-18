from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.main_db_migration as migration
from core.main_db_migration import (
    MIGRATION_ORDER,
    TABLE_SPECS,
    MainDbMigrationConfig,
    MainDbMigrator,
    _sanitize_grillo_activity_log_rows,
    audit_source_schema,
    build_postgres_upsert_sql,
    normalize_table_row,
    resolve_selected_tables,
)


def test_normalize_emotion_diary_row_preserves_legacy_numeric_id() -> None:
    normalized = normalize_table_row(
        "emotion_diary",
        {
            "id": 17,
            "source": "emotion_manager",
            "event": "update",
            "emotion": "happy",
            "intensity": 1,
            "state": "active",
            "trigger_condition": None,
            "decision_logic": None,
            "next_check": None,
        },
    )

    assert normalized["id"] == "emotion_17"
    assert normalized["legacy_numeric_id"] == 17
    assert normalized["intensity"] == 1.0
    assert normalized["timestamp"] is not None


def test_audit_source_schema_flags_legacy_emotion_diary() -> None:
    warnings = audit_source_schema(
        "emotion_diary",
        {
            "id": "varchar(100)",
            "intensity": "int(11)",
            "emotion": "varchar(50)",
        },
    )

    assert any("legacy text primary key" in warning for warning in warnings)
    assert any("double precision" in warning for warning in warnings)
    assert any("missing a timestamp column" in warning for warning in warnings)


def test_audit_source_schema_flags_ai_diary_user_message_width() -> None:
    warnings = audit_source_schema(
        "ai_diary",
        {
            "id": "int(11)",
            "user_message": "varchar(255)",
        },
    )

    assert any("user_message is width-limited" in warning for warning in warnings)


def test_build_postgres_upsert_sql_uses_named_conflict_keys() -> None:
    sql = build_postgres_upsert_sql(TABLE_SPECS["config"])

    assert 'ON CONFLICT ("config_key") DO UPDATE SET' in sql
    assert 'EXCLUDED."value"' in sql


def test_sanitize_grillo_activity_log_rows_nulls_orphaned_diary_refs() -> None:
    rows = [
        {"id": 1, "diary_entry_id": 1566},
        {"id": 2, "diary_entry_id": 10},
        {"id": 3, "diary_entry_id": None},
        {"id": 4, "diary_entry_id": "11"},
    ]

    sanitized = _sanitize_grillo_activity_log_rows(rows, {10, 11})

    assert sanitized == 1
    assert rows[0]["diary_entry_id"] is None
    assert rows[1]["diary_entry_id"] == 10
    assert rows[2]["diary_entry_id"] is None
    assert rows[3]["diary_entry_id"] == 11


def test_resolve_selected_tables_skips_emotion_diary_by_default() -> None:
    config = MainDbMigrationConfig(
        source_host="localhost",
        source_port=3306,
        source_user="synth",
        source_password="synth",
        source_database="synth",
        target_dsn="",
    )

    selected = resolve_selected_tables(config)

    assert selected == MIGRATION_ORDER
    assert "emotion_diary" not in selected


def test_resolve_selected_tables_can_include_emotion_diary() -> None:
    config = MainDbMigrationConfig(
        source_host="localhost",
        source_port=3306,
        source_user="synth",
        source_password="synth",
        source_database="synth",
        target_dsn="",
        include_legacy_emotion_diary=True,
    )

    selected = resolve_selected_tables(config)

    assert selected[:-1] == MIGRATION_ORDER
    assert selected[-1] == "emotion_diary"


@pytest.mark.asyncio
async def test_audit_only_run_skips_target_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.last_query = ""

        async def execute(self, query: str, params: object = None) -> None:
            del params
            self.last_query = query

        async def fetchone(self) -> object:
            if "SHOW TABLES LIKE" in self.last_query:
                return {"table": "config"}
            return None

        async def fetchall(self) -> list[dict[str, str]]:
            if "SHOW COLUMNS FROM `config`" in self.last_query:
                return [
                    {"Field": "config_key", "Type": "varchar(255)"},
                    {"Field": "value", "Type": "text"},
                ]
            return []

        async def __aenter__(self) -> "FakeCursor":
            return self

        async def __aexit__(
            self, exc_type: object, exc: object, traceback: object
        ) -> None:
            del exc_type, exc, traceback

    class FakeSourceConn:
        def cursor(self, *args: object, **kwargs: object) -> FakeCursor:
            del args, kwargs
            return FakeCursor()

        def close(self) -> None:
            return None

    async def fake_connect(**kwargs: object) -> FakeSourceConn:
        del kwargs
        return FakeSourceConn()

    monkeypatch.setattr(
        migration,
        "aiomysql",
        SimpleNamespace(DictCursor=object, connect=fake_connect),
    )
    monkeypatch.setattr(migration, "asyncpg", None)

    config = MainDbMigrationConfig(
        source_host="localhost",
        source_port=3306,
        source_user="synth",
        source_password="synth",
        source_database="synth",
        target_dsn="",
        audit_only=True,
        tables=("config",),
    )

    results = await MainDbMigrator(config).run()

    assert len(results) == 1
    assert results[0].name == "config"
    assert results[0].migrated_rows == 0
    assert results[0].skipped is False
