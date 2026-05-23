from unittest.mock import AsyncMock, MagicMock

import pytest

import plugins.radio_host.radio_host_plugin as radio_module
import plugins.radio_host.db as radio_db_module
import plugins.radio_host.jingle_injector as jingle_module


def _patch_radio_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        radio_module.config_registry,
        "get_value",
        lambda key, default=None, **kwargs: default,
    )
    monkeypatch.setattr(
        radio_module.config_registry,
        "add_listener",
        lambda key, listener: None,
    )


@pytest.mark.asyncio
async def test_start_continues_when_radio_table_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_radio_config(monkeypatch)

    plugin = radio_module.RadioHostPlugin()
    plugin._enabled = True

    async def fail_init_tables() -> None:
        raise RuntimeError("db unavailable")

    register_listeners = {"called": False}

    def fake_register_listeners() -> None:
        register_listeners["called"] = True

    ensure_webui = AsyncMock()
    ensure_running = AsyncMock()

    monkeypatch.setattr(radio_module, "init_radio_tables", fail_init_tables)
    monkeypatch.setattr(plugin, "_register_config_listeners", fake_register_listeners)
    monkeypatch.setattr(plugin, "_ensure_webui_routes_registered", ensure_webui)
    monkeypatch.setattr(plugin, "_ensure_running", ensure_running)

    await plugin.start()

    assert register_listeners["called"] is True
    ensure_webui.assert_awaited_once()
    ensure_running.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_radio_data_uses_in_memory_activity_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_radio_config(monkeypatch)

    plugin = radio_module.RadioHostPlugin()
    plugin._enabled = True
    plugin._running = True

    async def ok_init_tables() -> None:
        return None

    monkeypatch.setattr(radio_module, "init_radio_tables", ok_init_tables)

    def fail_get_conn_ctx():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("core.db.get_conn_ctx", fail_get_conn_ctx)

    await plugin._log_activity(
        track_title="Song B",
        track_artist="Artist B",
        banter_text="Live fallback banter",
        style="transition",
        status="success",
    )

    data = await plugin._build_radio_data()

    assert data["online"] is False
    assert len(data["activities"]) == 1
    assert data["activities"][0]["track_title"] == "Song B"
    assert data["activities"][0]["banter_text"] == "Live fallback banter"
    assert data["activities"][0]["status"] == "success"


# ---------------------------------------------------------------------------
# Vox registry key fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_tts_uses_vox_plugin_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate_tts must resolve the Vox plugin via 'vox_plugin', not 'vox'."""
    from plugins.radio_host.azuracast_client import AzuraCastClient
    import core.core_initializer as ci

    speak_called: list[str] = []

    async def fake_speak(
        text: str,
        engine_name: str | None = None,
        interface_path: str | None = None,
        **kwargs,
    ) -> dict:
        speak_called.append(text)
        return {"audio_path": "/tmp/fake.wav"}

    fake_vox = MagicMock()
    fake_vox.speak = fake_speak

    # Only register under "vox_plugin" — the legacy "vox" key must NOT be needed.
    registry: dict = {"vox_plugin": fake_vox}
    monkeypatch.setattr(ci, "PLUGIN_REGISTRY", registry)
    monkeypatch.setattr("os.path.isfile", lambda path: path == "/tmp/fake.wav")

    monkeypatch.setattr(
        radio_module.config_registry,
        "get_value",
        lambda key, default=None, **kwargs: default,
    )

    injector = jingle_module.JingleInjector(AzuraCastClient(), "test_station")
    result = await injector.generate_tts("Hello radio")

    assert result == "/tmp/fake.wav", (
        "generate_tts should succeed when VoxPlugin is registered as 'vox_plugin'"
    )
    assert speak_called == ["Hello radio"]


# ---------------------------------------------------------------------------
# DB schema dialect selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_radio_tables_uses_postgres_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Postgres the CREATE TABLE must use SERIAL and TIMESTAMPTZ."""
    # Reset the initialised flag so the function actually runs.
    monkeypatch.setattr(radio_db_module, "_table_initialized", False)
    monkeypatch.setattr(radio_db_module, "_get_db_type", lambda: "postgres")

    executed: list[str] = []

    class _FakeCursor:  # Postgres variant
        async def execute(self, sql: str, *args) -> None:
            executed.append(sql)

        async def fetchone(self) -> tuple:
            return (1,)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(radio_db_module, "get_conn_ctx", lambda: _FakeCtx())

    await radio_db_module.init_radio_tables()

    create_sql = next((s for s in executed if "CREATE TABLE" in s), "")
    assert "SERIAL" in create_sql, "Postgres schema must use SERIAL for the PK"
    assert "TIMESTAMPTZ" in create_sql, "Postgres schema must use TIMESTAMPTZ"
    assert "AUTO_INCREMENT" not in create_sql
    assert "DATETIME" not in create_sql


@pytest.mark.asyncio
async def test_init_radio_tables_uses_mariadb_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On MariaDB the CREATE TABLE must use INT AUTO_INCREMENT and DATETIME."""
    monkeypatch.setattr(radio_db_module, "_table_initialized", False)
    monkeypatch.setattr(radio_db_module, "_get_db_type", lambda: "mariadb")

    executed: list[str] = []

    class _FakeCursor:  # MariaDB variant
        def __init__(self) -> None:
            self._count_result: tuple = (
                1,
            )  # simulate column already exists → skip ALTER

        async def execute(self, sql: str, *args) -> None:
            executed.append(sql)

        async def fetchone(self) -> tuple:
            return self._count_result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(radio_db_module, "get_conn_ctx", lambda: _FakeCtx())

    await radio_db_module.init_radio_tables()

    create_sql = next((s for s in executed if "CREATE TABLE" in s), "")
    assert "AUTO_INCREMENT" in create_sql, "MariaDB schema must use INT AUTO_INCREMENT"
    assert "DATETIME" in create_sql, "MariaDB schema must use DATETIME"
    assert "SERIAL" not in create_sql
    assert "TIMESTAMPTZ" not in create_sql
