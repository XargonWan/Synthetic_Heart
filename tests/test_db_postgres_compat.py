from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

import core.db as db_module
from core.external_endpoints.registry import ExternalEndpointRegistry
from core.db_backends import translate_postgres_sql


def test_translate_postgres_sql_strips_mysql_ddl_bits() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS config (
        `config_key` VARCHAR(255) PRIMARY KEY,
        `value` TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_config (`config_key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """

    translated = translate_postgres_sql(sql)

    assert "ENGINE=InnoDB" not in translated
    assert "ON UPDATE CURRENT_TIMESTAMP" not in translated
    assert "TIMESTAMPTZ" in translated
    assert (
        'UNIQUE ("config_key")' in translated or 'UNIQUE ("config_key")' in translated
    )


def test_primary_db_selector_forces_soul_connection_settings(monkeypatch) -> None:
    monkeypatch.setenv("SYNTH_PRIMARY_DB", "soul")
    monkeypatch.setenv("DB_HOST", "memory-db")
    monkeypatch.setenv("DB_PORT", "3306")
    monkeypatch.setenv("DB_USER", "memory-user")
    monkeypatch.setenv("DB_PASS", "memory-pass")
    monkeypatch.setenv("DB_NAME", "memory-db")
    monkeypatch.setenv(
        "SOUL_POSTGRES_DSN",
        "postgresql://soul_user:soul_pass@soul-db.example:5544/soul_main",
    )
    monkeypatch.delenv("SOUL_PG_HOST", raising=False)
    monkeypatch.delenv("SOUL_PG_PORT", raising=False)
    monkeypatch.delenv("SOUL_PG_USER", raising=False)
    monkeypatch.delenv("SOUL_PG_PASSWORD", raising=False)
    monkeypatch.delenv("SOUL_PG_DB", raising=False)

    assert db_module._get_db_type() == "postgres"
    assert db_module._get_db_dsn() == (
        "postgresql://soul_user:soul_pass@soul-db.example:5544/soul_main"
    )
    assert db_module._read_db_config() == (
        "soul-db.example",
        5544,
        "soul_user",
        "soul_pass",
        "soul_main",
    )


def test_primary_db_selector_falls_back_to_general_db_settings_when_soul_settings_are_empty(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SYNTH_PRIMARY_DB", "soul")
    monkeypatch.setenv("DB_HOST", "fallback-db")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_USER", "fallback-user")
    monkeypatch.setenv("DB_PASS", "fallback-pass")
    monkeypatch.setenv("DB_NAME", "fallback-db")
    monkeypatch.delenv("SOUL_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_DSN", raising=False)
    monkeypatch.delenv("SOUL_PG_HOST", raising=False)
    monkeypatch.delenv("SOUL_PG_PORT", raising=False)
    monkeypatch.delenv("SOUL_PG_USER", raising=False)
    monkeypatch.delenv("SOUL_PG_PASSWORD", raising=False)
    monkeypatch.delenv("SOUL_PG_DB", raising=False)

    assert db_module._get_db_type() == "postgres"
    assert db_module._read_db_config() == (
        "fallback-db",
        5432,
        "fallback-user",
        "fallback-pass",
        "fallback-db",
    )


def test_primary_db_selector_falls_back_to_postgres_default_port_when_db_port_is_mysql(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SYNTH_PRIMARY_DB", "soul")
    monkeypatch.setenv("DB_HOST", "fallback-db")
    monkeypatch.setenv("DB_PORT", "3306")
    monkeypatch.setenv("DB_USER", "fallback-user")
    monkeypatch.setenv("DB_PASS", "fallback-pass")
    monkeypatch.setenv("DB_NAME", "fallback-db")
    monkeypatch.delenv("SOUL_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_DSN", raising=False)
    monkeypatch.delenv("SOUL_PG_HOST", raising=False)
    monkeypatch.delenv("SOUL_PG_PORT", raising=False)
    monkeypatch.delenv("SOUL_PG_USER", raising=False)
    monkeypatch.delenv("SOUL_PG_PASSWORD", raising=False)
    monkeypatch.delenv("SOUL_PG_DB", raising=False)

    assert db_module._get_db_type() == "postgres"
    assert db_module._read_db_config() == (
        "fallback-db",
        5432,
        "fallback-user",
        "fallback-pass",
        "fallback-db",
    )


def test_primary_db_selector_forces_memory_connection_settings(monkeypatch) -> None:
    monkeypatch.setenv("SYNTH_PRIMARY_DB", "memory")
    monkeypatch.setenv("DB_HOST", "memory-db")
    monkeypatch.setenv("DB_PORT", "3306")
    monkeypatch.setenv("DB_USER", "memory-user")
    monkeypatch.setenv("DB_PASS", "memory-pass")
    monkeypatch.setenv("DB_NAME", "memory-main")
    monkeypatch.setenv(
        "SOUL_POSTGRES_DSN",
        "postgresql://soul_user:soul_pass@soul-db.example:5544/soul_main",
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://other:other@other-host:5432/other")

    assert db_module._get_db_type() == "mariadb"
    assert db_module._get_db_dsn() is None
    assert db_module._read_db_config() == (
        "memory-db",
        3306,
        "memory-user",
        "memory-pass",
        "memory-main",
    )


def test_db_type_defaults_to_postgres(monkeypatch) -> None:
    monkeypatch.delenv("SYNTH_PRIMARY_DB", raising=False)
    monkeypatch.delenv("SYNTH_DB_TYPE", raising=False)
    monkeypatch.delenv("DB_TYPE", raising=False)

    assert db_module._get_db_type() == "postgres"


@pytest.mark.asyncio
async def test_get_conn_ctx_postgres_translates_replace_into(monkeypatch) -> None:
    calls: list[tuple[str, str, tuple[object, ...]]] = []

    class FakePgConn:
        async def execute(self, sql, *args):
            calls.append(("execute", sql, args))
            return "INSERT 0 1"

        async def fetch(self, sql, *args):
            calls.append(("fetch", sql, args))
            return []

    class FakePool:
        def __init__(self):
            self.raw = FakePgConn()
            self.released = 0

        async def acquire(self):
            return self.raw

        def release(self, conn):
            assert conn is self.raw
            self.released += 1

    fake_pool = FakePool()

    async def fake_get_pool():
        return fake_pool

    monkeypatch.setattr(db_module, "get_pool", fake_get_pool)
    monkeypatch.setattr(db_module, "_get_db_type", lambda: "postgres")

    async with db_module.get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "REPLACE INTO config (`config_key`, `value`) VALUES (%s, %s)",
                ("BASE_CORTEX", "postgres-main"),
            )

    assert fake_pool.released == 1
    assert calls[0][0] == "execute"
    assert "SET statement_timeout = 30000" in calls[0][1]

    translated_query = calls[1][1]
    assert 'ON CONFLICT ("config_key") DO UPDATE SET' in translated_query
    assert 'EXCLUDED."value"' in translated_query
    assert calls[1][2] == ("BASE_CORTEX", "postgres-main")


@pytest.mark.asyncio
async def test_get_conn_ctx_postgres_awaits_async_pool_release(monkeypatch) -> None:
    class FakePgConn:
        async def execute(self, sql, *args):
            return "SELECT 1"

        async def fetch(self, sql, *args):
            return []

    class FakePool:
        def __init__(self):
            self.raw = FakePgConn()
            self.released = 0

        async def acquire(self):
            return self.raw

        async def release(self, conn):
            assert conn is self.raw
            self.released += 1

    fake_pool = FakePool()

    async def fake_get_pool():
        return fake_pool

    monkeypatch.setattr(db_module, "get_pool", fake_get_pool)
    monkeypatch.setattr(db_module, "_get_db_type", lambda: "postgres")

    async with db_module.get_conn_ctx():
        pass

    assert fake_pool.released == 1


@pytest.mark.asyncio
async def test_get_due_events_uses_boolean_false_on_postgres(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCursor:
        async def fetchall(self):
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self, *args, **kwargs):
            return FakeCursor()

    class FakeConnCtx:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_safe_db_execute(cur, query, params=(), ensure_fn=None):
        captured["query"] = query
        captured["params"] = params

    monkeypatch.setattr(db_module, "_get_db_type", lambda: "postgres")
    monkeypatch.setattr(db_module, "get_conn_ctx", lambda: FakeConnCtx())
    monkeypatch.setattr(db_module, "safe_db_execute", fake_safe_db_execute)

    result = await db_module.get_due_events(
        now=datetime(2026, 4, 18, tzinfo=timezone.utc)
    )

    assert result == []
    assert captured["query"] == (
        "SELECT * FROM scheduled_events WHERE delivered = FALSE AND next_run <= %s ORDER BY id"
    )
    assert captured["params"] == (datetime(2026, 4, 18, 0, 3, tzinfo=timezone.utc),)


@pytest.mark.asyncio
async def test_insert_scheduled_event_uses_datetime_param_on_postgres(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self, *args, **kwargs):
            return FakeCursor()

    class FakeConnCtx:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_safe_db_execute(cur, query, params=(), ensure_fn=None):
        captured["query"] = query
        captured["params"] = params

    async def fake_ensure_core_tables():
        return None

    monkeypatch.setattr(db_module, "_get_db_type", lambda: "postgres")
    monkeypatch.setattr(db_module, "get_conn_ctx", lambda: FakeConnCtx())
    monkeypatch.setattr(db_module, "safe_db_execute", fake_safe_db_execute)
    monkeypatch.setattr(db_module, "ensure_core_tables", fake_ensure_core_tables)
    # insert_scheduled_event now converts the local wall-clock time to UTC
    # internally using the system timezone. Pin it to UTC so the expected
    # next_run is deterministic (14:34 local == 14:34 UTC).
    monkeypatch.setattr(
        "core.time_zone_utils.get_local_timezone", lambda: timezone.utc
    )

    await db_module.insert_scheduled_event(
        date="2026-04-18",
        time="14:34",
        recurrence_type="none",
        description="test event",
    )

    assert "INSERT INTO scheduled_events" in captured["query"]
    # On Postgres, next_run (param index 2) must be a native datetime object,
    # not a preformatted string.
    next_run_param = captured["params"][2]
    assert isinstance(next_run_param, datetime)
    assert next_run_param == datetime(2026, 4, 18, 14, 34, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_heal_cortex_config_preserves_valid_builtin_engine(monkeypatch) -> None:
    class FakeRegistry:
        def get_available_engines(self) -> list[str]:
            return ["anthropic", "gemini_api", "manual"]

        def get_default_engine(self) -> str:
            return "manual"

    class FakeCursor:
        def __init__(self) -> None:
            self._rows: list[tuple[str, ...]] = []
            self.calls: list[tuple[str, object]] = []

        async def execute(self, query, params=None):
            self.calls.append((query, params))
            if "SELECT name FROM external_endpoints" in query:
                self._rows = [("anthropic",)]
            elif "SELECT config_key, value FROM config" in query:
                self._rows = [
                    ("BASE_CORTEX", "gemini_api"),
                    ("GRILLO_CORTEX", "gemini_api"),
                    ("TRAINER_CORTEX", "Default"),
                ]
            else:
                self._rows = []

        async def fetchall(self):
            rows = self._rows
            self._rows = []
            return rows

    fake_cursor = FakeCursor()

    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )

    await db_module._heal_cortex_config(fake_cursor)

    assert not any(
        "UPDATE config SET value = %s WHERE config_key = %s" in query
        for query, _ in fake_cursor.calls
    )


@pytest.mark.asyncio
async def test_heal_cortex_config_repairs_invalid_values(monkeypatch) -> None:
    class FakeRegistry:
        def get_available_engines(self) -> list[str]:
            return ["anthropic", "gemini_api", "manual"]

        def get_default_engine(self) -> str:
            return "manual"

    class FakeCursor:
        def __init__(self) -> None:
            self._rows: list[tuple[str, ...]] = []
            self.calls: list[tuple[str, object]] = []

        async def execute(self, query, params=None):
            self.calls.append((query, params))
            if "SELECT name FROM external_endpoints" in query:
                self._rows = [("anthropic",)]
            elif "SELECT config_key, value FROM config" in query:
                self._rows = [
                    ("BASE_CORTEX", "removed_engine"),
                    ("GRILLO_CORTEX", "removed_engine"),
                    ("TRAINER_CORTEX", "Default"),
                ]
            else:
                self._rows = []

        async def fetchall(self):
            rows = self._rows
            self._rows = []
            return rows

    fake_cursor = FakeCursor()

    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )

    await db_module._heal_cortex_config(fake_cursor)

    assert (
        "UPDATE config SET value = %s WHERE config_key = %s",
        ("Default", "GRILLO_CORTEX"),
    ) in fake_cursor.calls
    assert (
        "UPDATE config SET value = %s WHERE config_key = %s",
        ("anthropic", "BASE_CORTEX"),
    ) in fake_cursor.calls


@pytest.mark.asyncio
async def test_heal_cortex_config_skips_when_registry_is_uninitialized(
    monkeypatch,
) -> None:
    class FakeRegistry:
        def get_available_engines(self) -> list[str]:
            return []

    class FakeCursor:
        def __init__(self) -> None:
            self._rows: list[tuple[str, ...]] = []
            self.calls: list[tuple[str, object]] = []

        async def execute(self, query, params=None):
            self.calls.append((query, params))
            self._rows = []

        async def fetchall(self):
            rows = self._rows
            self._rows = []
            return rows

    fake_cursor = FakeCursor()

    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )

    await db_module._heal_cortex_config(fake_cursor)

    assert fake_cursor.calls == []


@pytest.mark.asyncio
async def test_list_endpoints_uses_boolean_filter_for_enabled_only(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class FakeCursor:
        async def execute(self, query, params=None):
            captured["query"] = query

        async def fetchall(self):
            return [
                {
                    "id": 2,
                    "name": "gemini",
                    "display_label": "Gemini",
                    "protocol": "gemini",
                    "base_url": "https://example.invalid",
                    "api_key_enc": None,
                    "enabled": True,
                    "capabilities": {},
                    "subsystem_map": {"cortex": True},
                    "available_models": [],
                    "default_model": "gemini-3-flash-preview",
                    "probe_status": "success",
                    "last_probe_at": None,
                    "extra_config": {},
                }
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self, *args, **kwargs):
            return FakeCursor()

    class FakeConnCtx:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    registry = ExternalEndpointRegistry()

    monkeypatch.setattr(
        "core.external_endpoints.registry._ensure_table", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "core.db.get_conn_ctx",
        lambda: FakeConnCtx(),
    )

    result = await registry.list_endpoints(enabled_only=True)

    assert len(result) == 1
    assert result[0].name == "gemini"
    assert (
        captured["query"]
        == "SELECT * FROM external_endpoints WHERE enabled ORDER BY id"
    )


@pytest.mark.asyncio
async def test_set_probe_result_uses_datetime_objects_for_probe_timestamps(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeCursor:
        async def execute(self, query, params=None):
            captured["query"] = query
            captured["params"] = params

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self, *args, **kwargs):
            return FakeCursor()

        async def commit(self):
            captured["committed"] = True

    class FakeConnCtx:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    registry = ExternalEndpointRegistry()

    monkeypatch.setattr(
        "core.external_endpoints.registry._ensure_table", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: FakeConnCtx())
    monkeypatch.setattr(
        registry,
        "get_endpoint",
        AsyncMock(return_value=None),
    )

    await registry.set_probe_result(
        endpoint_id=5,
        status="success",
        capabilities={"cortex": True},
        models=["gemini-3-flash-preview"],
    )

    params = captured["params"]
    assert isinstance(params, tuple)
    assert params[0] == "success"
    assert params[1] == '{"cortex": true}'
    assert params[2] == '["gemini-3-flash-preview"]'
    assert params[3] == "[]"
    assert isinstance(params[4], datetime)
    assert isinstance(params[5], datetime)
    assert params[4].tzinfo == timezone.utc
    assert params[5].tzinfo == timezone.utc
    assert params[6] == 5
    assert captured["committed"] is True


@pytest.mark.asyncio
async def test_auto_set_default_model_uses_datetime_objects(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCursor:
        async def execute(self, query, params=None):
            captured["query"] = query
            captured["params"] = params

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self, *args, **kwargs):
            return FakeCursor()

        async def commit(self):
            captured["committed"] = True

    class FakeConnCtx:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    registry = ExternalEndpointRegistry()

    monkeypatch.setattr("core.db.get_conn_ctx", lambda: FakeConnCtx())

    await registry._auto_set_default_model(5, "gemini-3-flash-preview")

    params = captured["params"]
    assert isinstance(params, tuple)
    assert params[0] == "gemini-3-flash-preview"
    assert isinstance(params[1], datetime)
    assert params[1].tzinfo == timezone.utc
    assert params[2] == 5
    assert captured["committed"] is True


@pytest.mark.asyncio
async def test_set_default_model_uses_datetime_objects(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCursor:
        async def execute(self, query, params=None):
            captured["query"] = query
            captured["params"] = params

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self, *args, **kwargs):
            return FakeCursor()

        async def commit(self):
            captured["committed"] = True

    class FakeConnCtx:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    registry = ExternalEndpointRegistry()

    monkeypatch.setattr(
        "core.external_endpoints.registry._ensure_table", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: FakeConnCtx())

    await registry.set_default_model(5, "gemini-3-flash-preview")

    params = captured["params"]
    assert isinstance(params, tuple)
    assert params[0] == "gemini-3-flash-preview"
    assert isinstance(params[1], datetime)
    assert params[1].tzinfo == timezone.utc
    assert params[2] == 5
    assert captured["committed"] is True
