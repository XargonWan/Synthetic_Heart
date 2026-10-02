"""The memories label-column widen: align a lived-in store with the declared schema.

``scripts/sql/app_main_postgres.sql`` types ``emotion``/``scope``/``emotion_state``/``author``/
``source`` as TEXT, but the live store still carries varchar(50)/varchar(100). The compactor's
failing writes were the symptom; this migration is the schema half of the fix.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from core.migrations import _widen_memories_text_columns

# Column -> (declared max length, nullable, default) as information_schema would answer it.
_COLUMNS = {
    "emotion": (50, "YES", None),
    "emotion_state": (None, "YES", None),  # already TEXT: must be left alone
    "scope": (50, "YES", None),
    "author": (100, "NO", None),
    "source": (100, "YES", None),
}


class _Cursor:
    def __init__(self, executed: list[str]):
        self.executed = executed
        self._sql = ""
        self._params: tuple = ()

    async def execute(self, sql, params=None):
        self._sql = " ".join(str(sql).split())
        self._params = tuple(params or ())
        self.executed.append(self._sql)
        return None

    async def fetchone(self):
        if self._sql.startswith("SELECT to_regclass"):
            return (True,)
        if "COUNT(*) FROM information_schema" in self._sql:
            return (1,)  # the table and every column exist
        if self._sql.startswith("SELECT character_maximum_length"):
            column = self._params[1] if len(self._params) > 1 else ""
            return _COLUMNS.get(column, (None, "YES", None))
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Conn:
    def __init__(self, executed: list[str]):
        self._executed = executed

    def cursor(self):
        return _Cursor(self._executed)

    async def commit(self):
        return None


def _patch_db(monkeypatch, executed: list[str], db_type: str = "postgres") -> None:
    import core.db as core_db

    @asynccontextmanager
    async def fake_conn_ctx():
        yield _Conn(executed)

    monkeypatch.setattr(core_db, "get_conn_ctx", fake_conn_ctx)
    monkeypatch.setattr(core_db, "_get_db_type", lambda: db_type)


def _alters(executed: list[str]) -> list[str]:
    return [sql for sql in executed if sql.startswith("ALTER TABLE")]


@pytest.mark.asyncio
async def test_only_bounded_columns_are_widened(monkeypatch):
    executed: list[str] = []
    _patch_db(monkeypatch, executed)

    await _widen_memories_text_columns()

    assert _alters(executed) == [
        'ALTER TABLE "memories" ALTER COLUMN "emotion" TYPE text',
        'ALTER TABLE "memories" ALTER COLUMN "scope" TYPE text',
        'ALTER TABLE "memories" ALTER COLUMN "author" TYPE text',
        'ALTER TABLE "memories" ALTER COLUMN "source" TYPE text',
    ]
    # emotion_state is already TEXT: a no-op, never a rewrite.
    assert all("emotion_state" not in sql for sql in _alters(executed))


@pytest.mark.asyncio
async def test_a_store_that_matches_the_schema_is_left_alone(monkeypatch):
    executed: list[str] = []
    _patch_db(monkeypatch, executed)

    async def fake_definition(cur, table, column, db_type):
        return (None, True, False)  # every column already unbounded

    monkeypatch.setattr("core.migrations._column_definition", fake_definition)

    await _widen_memories_text_columns()

    assert _alters(executed) == []


@pytest.mark.asyncio
async def test_a_missing_table_short_circuits(monkeypatch):
    executed: list[str] = []
    _patch_db(monkeypatch, executed)

    async def fake_table_exists(cur, table, db_type):
        return False

    monkeypatch.setattr("core.migrations._table_exists", fake_table_exists)

    await _widen_memories_text_columns()

    assert _alters(executed) == []


@pytest.mark.asyncio
async def test_a_mysql_store_keeps_nullability(monkeypatch):
    executed: list[str] = []
    _patch_db(monkeypatch, executed, db_type="mysql")

    async def fake_definition(cur, table, column, db_type):
        if column == "emotion":
            return (50, False, None)  # NOT NULL, as the real helper reports it
        return (None, True, False)

    monkeypatch.setattr("core.migrations._column_definition", fake_definition)

    await _widen_memories_text_columns()

    assert _alters(executed) == [
        "ALTER TABLE `memories` MODIFY `emotion` TEXT NOT NULL"
    ]


@pytest.mark.asyncio
async def test_a_mysql_column_with_a_default_is_left_alone(monkeypatch):
    """Restating a backend-specific default from a MODIFY is riskier than the width."""
    executed: list[str] = []
    _patch_db(monkeypatch, executed, db_type="mysql")

    async def fake_definition(cur, table, column, db_type):
        return (50, "YES", "''") if column == "emotion" else (None, True, False)

    monkeypatch.setattr("core.migrations._column_definition", fake_definition)

    await _widen_memories_text_columns()

    assert _alters(executed) == []
