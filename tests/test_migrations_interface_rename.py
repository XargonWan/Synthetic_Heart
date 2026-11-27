"""Renaming an interface in every stored path (ollama_serve -> openai_api)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from core.migrations import _migrate_ollama_serve_to_openai_api, _rename_interface


class _Cursor:
    def __init__(self, log: list[tuple[str, tuple[Any, ...]]], missing: set[str]):
        self.log, self.missing = log, missing
        self._sql, self._params = "", ()

    async def execute(self, sql: str, params: Any = None) -> None:
        self._sql = " ".join(str(sql).split())
        self._params = tuple(params or ())
        if self._sql.startswith("UPDATE") and any(
            f'"{t}"' in self._sql or f"`{t}`" in self._sql for t in self.missing
        ):
            raise RuntimeError("relation does not exist")
        self.log.append((self._sql, self._params))

    async def fetchone(self) -> tuple[bool]:
        # _table_exists: to_regclass('public.<table>') IS NOT NULL
        table = self._params[0].split(".")[-1] if self._params else ""
        return (table not in self.missing,)

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _Conn:
    def __init__(self, log: list[tuple[str, tuple[Any, ...]]], missing: set[str]):
        self.log, self.missing, self.commits = log, missing, 0

    def cursor(self) -> _Cursor:
        return _Cursor(self.log, self.missing)

    async def commit(self) -> None:
        self.commits += 1


def _patch(monkeypatch: pytest.MonkeyPatch, missing: set[str] | None = None):
    import core.db as core_db

    log: list[tuple[str, tuple[Any, ...]]] = []
    conn = _Conn(log, missing or set())

    @asynccontextmanager
    async def ctx():
        yield conn

    monkeypatch.setattr(core_db, "get_conn_ctx", ctx)
    monkeypatch.setattr(core_db, "_get_db_type", lambda: "postgres")
    return log, conn


@pytest.mark.asyncio
async def test_every_path_column_and_the_alias_table_are_renamed(monkeypatch) -> None:
    log, conn = _patch(monkeypatch)
    done = await _rename_interface("ollama_serve", "openai_api")
    updates = [(sql, params) for sql, params in log if sql.startswith("UPDATE")]
    assert done == len(updates) == 5
    path_updates = [u for u in updates if "CONCAT" in u[0]]
    assert len(path_updates) == 4
    for sql, params in path_updates:
        # "ollama_serve/" is 13 characters: keep everything from the 14th on.
        assert params == ("openai_api/", 14, 13, "ollama_serve/")
        assert "SUBSTR(" in sql and "LIKE" not in sql  # '_' is a LIKE wildcard
    assert updates[-1] == (
        "UPDATE interface_aliases SET interface = %s WHERE interface = %s",
        ("openai_api", "ollama_serve"),
    )
    assert conn.commits == 1


@pytest.mark.asyncio
async def test_the_prefix_is_typed_for_postgres_but_not_for_mariadb(
    monkeypatch,
) -> None:
    """PostgreSQL cannot tell the type of a bare parameter inside CONCAT: the migration
    failed on a real database with "could not determine data type of parameter $1"."""
    import core.db as core_db

    log, _ = _patch(monkeypatch)
    await _rename_interface("ollama_serve", "openai_api")
    assert all("CONCAT(%s::text," in sql for sql, _ in log if "CONCAT" in sql)
    log.clear()
    monkeypatch.setattr(core_db, "_get_db_type", lambda: "mysql")
    await _rename_interface("ollama_serve", "openai_api")
    sqls = [sql for sql, _ in log if "CONCAT" in sql]
    assert sqls and all("CONCAT(%s," in sql and "::text" not in sql for sql in sqls)


@pytest.mark.asyncio
async def test_a_missing_table_does_not_stop_the_others(monkeypatch) -> None:
    log, _ = _patch(monkeypatch, missing={"turn_reason_trail", "web_search_tasks"})
    assert await _rename_interface("ollama_serve", "openai_api") == 3
    touched = " ".join(sql for sql, _ in log if sql.startswith("UPDATE"))
    assert "chat_history_cache" in touched and "turn_reason_trail" not in touched


@pytest.mark.asyncio
async def test_the_registered_migration_uses_the_new_name(monkeypatch) -> None:
    log, _ = _patch(monkeypatch)
    await _migrate_ollama_serve_to_openai_api()
    assert any(params[-1:] == ("ollama_serve/",) for _, params in log)


def test_the_migration_is_registered() -> None:
    from core.migrations import _STARTUP_MIGRATIONS

    assert "migrate_ollama_serve_to_openai_api" in dict(_STARTUP_MIGRATIONS)


class _EndpointCursor(_Cursor):
    def __init__(self, log, missing, names: set[str]):
        super().__init__(log, missing)
        self.names = names

    async def execute(self, sql: str, params: Any = None) -> None:
        await super().execute(sql, params)
        self._rows: list[Any] = []
        if self._sql.startswith("SELECT name FROM external_endpoints"):
            self._rows = [(n,) for n in self.names if n in tuple(params or ())]

    async def fetchall(self) -> list[Any]:
        return self._rows


def _patch_endpoints(monkeypatch: pytest.MonkeyPatch, names: set[str]):
    import core.db as core_db

    log: list[tuple[str, tuple[Any, ...]]] = []

    class Conn(_Conn):
        def cursor(self) -> _Cursor:
            return _EndpointCursor(self.log, self.missing, names)

    conn = Conn(log, set())

    @asynccontextmanager
    async def ctx():
        yield conn

    monkeypatch.setattr(core_db, "get_conn_ctx", ctx)
    monkeypatch.setattr(core_db, "_get_db_type", lambda: "postgres")
    return log


@pytest.mark.asyncio
async def test_an_endpoint_and_the_settings_naming_it_are_renamed(monkeypatch) -> None:
    from core.migrations import _rename_endpoint

    log = _patch_endpoints(monkeypatch, {"selenium-llm-engine"})
    assert await _rename_endpoint("selenium-llm-engine", "zen-llm-engine") is True
    updates = [(s, p) for s, p in log if s.startswith("UPDATE")]
    assert updates[0] == (
        "UPDATE external_endpoints SET name = %s WHERE name = %s",
        ("zen-llm-engine", "selenium-llm-engine"),
    )
    assert "config_key IN" in updates[1][0]
    assert updates[1][1][:2] == ("zen-llm-engine", "selenium-llm-engine")
    assert "BASE_CORTEX" in updates[1][1] and "ACTIVE_IRIS_ENGINE" in updates[1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "names",
    [set(), {"zen-llm-engine"}, {"selenium-llm-engine", "zen-llm-engine"}],
)
async def test_nothing_is_renamed_when_it_would_merge_or_is_absent(
    monkeypatch, names: set[str]
) -> None:
    from core.migrations import _rename_endpoint

    log = _patch_endpoints(monkeypatch, names)
    assert await _rename_endpoint("selenium-llm-engine", "zen-llm-engine") is False
    assert not any(s.startswith("UPDATE") for s, _ in log)


class _TrainerCursor(_Cursor):
    def __init__(self, log, missing, value: str | None):
        super().__init__(log, missing)
        self.value = value
        self._row: Any = None

    async def execute(self, sql: str, params: Any = None) -> None:
        await super().execute(sql, params)
        self._row = None
        if self._sql.startswith("SELECT value FROM config"):
            self._row = None if self.value is None else (self.value,)

    async def fetchone(self) -> Any:
        if self._sql.startswith("SELECT value FROM config"):
            return self._row
        return await super().fetchone()


def _patch_trainers(monkeypatch: pytest.MonkeyPatch, value: str | None):
    import core.db as core_db

    log: list[tuple[str, tuple[Any, ...]]] = []

    class Conn(_Conn):
        def cursor(self) -> _Cursor:
            return _TrainerCursor(self.log, self.missing, value)

    conn = Conn(log, set())

    @asynccontextmanager
    async def ctx():
        yield conn

    monkeypatch.setattr(core_db, "get_conn_ctx", ctx)
    monkeypatch.setattr(core_db, "_get_db_type", lambda: "postgres")
    return log


def _written(log) -> list[Any]:
    return [
        params
        for sql, params in log
        if sql.startswith("UPDATE config SET value = %s WHERE config_key = %s")
    ]


@pytest.mark.asyncio
async def test_the_matrix_trainer_and_stored_paths_follow_the_new_id(
    monkeypatch,
) -> None:
    from core.migrations import _migrate_matrix_chat_to_matrix

    log = _patch_trainers(
        monkeypatch, "telegram_bot:5208,matrix_chat:@boss:hs,discord_bot:7"
    )
    await _migrate_matrix_chat_to_matrix()
    assert _written(log) == [
        ("telegram_bot:5208,matrix:@boss:hs,discord_bot:7", "TRAINER_IDS")
    ]
    assert any(
        params[-1:] == ("matrix_chat/",) for _, params in log
    )  # the stored paths too


@pytest.mark.asyncio
async def test_a_trainer_already_set_for_the_new_id_wins(monkeypatch) -> None:
    from core.migrations import _migrate_matrix_chat_to_matrix

    log = _patch_trainers(monkeypatch, "matrix_chat:@old:hs,matrix:@new:hs")
    await _migrate_matrix_chat_to_matrix()
    assert _written(log) == [("matrix:@new:hs", "TRAINER_IDS")]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "telegram_bot:5", "matrix:@boss:hs"])
async def test_nothing_to_rename_writes_nothing(monkeypatch, value) -> None:
    from core.migrations import _migrate_matrix_chat_to_matrix

    log = _patch_trainers(monkeypatch, value)
    await _migrate_matrix_chat_to_matrix()
    assert _written(log) == []


def test_the_matrix_migration_is_registered() -> None:
    from core.migrations import _STARTUP_MIGRATIONS

    assert "migrate_matrix_chat_to_matrix" in dict(_STARTUP_MIGRATIONS)
