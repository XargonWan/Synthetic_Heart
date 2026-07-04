from unittest.mock import AsyncMock

import pytest

import main as main_module
from core.config_manager import config_registry


@pytest.mark.asyncio
async def test_initialize_database_uses_postgres_permission_query(monkeypatch) -> None:
    executed_sql: list[str] = []

    class FakeCursor:
        async def execute(self, sql: str, *args, **kwargs) -> None:
            executed_sql.append(sql)

        async def fetchall(self) -> list[dict[str, object]]:
            return [
                {
                    "role_name": "soul",
                    "database_name": "soul",
                    "can_connect": True,
                    "can_create": True,
                    "public_usage": True,
                    "public_create": True,
                }
            ]

        async def __aenter__(self) -> "FakeCursor":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class FakeConn:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    class FakeConnCtx:
        async def __aenter__(self) -> FakeConn:
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    test_connection = AsyncMock(return_value=True)
    init_db = AsyncMock()
    persist_bootstrap_configs = AsyncMock()

    monkeypatch.setattr(main_module, "_get_db_type", lambda: "postgres")
    monkeypatch.setattr(main_module, "get_conn_ctx", lambda: FakeConnCtx())
    monkeypatch.setattr(main_module, "test_connection", test_connection)
    monkeypatch.setattr(main_module, "init_db", init_db)
    monkeypatch.setattr(
        config_registry,
        "persist_bootstrap_configs",
        persist_bootstrap_configs,
    )

    result = await main_module.initialize_database()

    assert result is True
    assert any("has_database_privilege" in sql for sql in executed_sql)
    assert all("SHOW GRANTS" not in sql for sql in executed_sql)
    test_connection.assert_awaited_once()
    init_db.assert_awaited_once()
    persist_bootstrap_configs.assert_awaited_once()
