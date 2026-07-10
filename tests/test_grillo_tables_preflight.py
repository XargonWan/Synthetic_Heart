"""Tests for the grillo audit table preflight (core.db.init_grillo_tables).

The Postgres runtime historically never got grillo_activity_log /
grillo_action_execs because their DDL lived only in the MariaDB branch of
ensure_plugin_tables — every beat then ran as a black box (no History >
Grillo, no dream recall, no suppression notes, activity_id always None).
"""

import pytest

import core.db as cdb
from core.db_backends import translate_postgres_sql


class _RecordingCursor:
    def __init__(self, executed: list):
        self._executed = executed

    async def execute(self, sql, params=None):
        self._executed.append(sql)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingConn:
    def __init__(self, executed: list):
        self._executed = executed
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _RecordingCursor(self._executed)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "db_type,expected_statements",
    [
        ("mariadb", 2),  # two CREATE TABLEs, indexes are inline in the DDL
        ("postgres", 6),  # two CREATE TABLEs + four explicit CREATE INDEX
    ],
)
async def test_init_grillo_tables_statement_count(
    monkeypatch, db_type, expected_statements
):
    executed: list = []
    conn = _RecordingConn(executed)

    monkeypatch.setattr(cdb, "get_conn_ctx", lambda: conn)
    monkeypatch.setattr(cdb, "_get_db_type", lambda: db_type)

    await cdb.init_grillo_tables()

    assert len(executed) == expected_statements
    assert "grillo_activity_log" in executed[0]
    assert "grillo_action_execs" in executed[1]
    assert conn.committed
    if db_type == "postgres":
        assert all("CREATE INDEX IF NOT EXISTS" in sql for sql in executed[2:])


def test_grillo_ddl_translates_cleanly_to_postgres():
    """The MariaDB-dialect DDL must come out of the translator as valid
    Postgres: no MySQL-isms left, identity/JSONB in place, inline INDEX
    lines stripped (they are recreated via _GRILLO_PG_INDEX_DDL)."""
    for ddl in (cdb._GRILLO_ACTIVITY_LOG_DDL, cdb._GRILLO_ACTION_EXECS_DDL):
        translated = translate_postgres_sql(ddl)
        assert "AUTO_INCREMENT" not in translated
        assert "SERIAL PRIMARY KEY" in translated
        assert "ENGINE" not in translated
        assert "ENUM" not in translated.upper() or "TEXT" in translated
        assert "JSONB" in translated
        assert "\n INDEX" not in translated and "INDEX idx_" not in translated

    activity = translate_postgres_sql(cdb._GRILLO_ACTIVITY_LOG_DDL)
    assert "TIMESTAMPTZ" in activity
    # The literal column names must survive translation intact
    assert "executed_at" in activity
    assert "suppressed_count" in activity

    execs = translate_postgres_sql(cdb._GRILLO_ACTION_EXECS_DDL)
    assert "ON UPDATE CURRENT_TIMESTAMP" not in execs
    assert "activity_log_id" in execs
