import sys
import builtins
import asyncio
import pytest
from unittest.mock import AsyncMock


def test_aiomysql_import_fails(monkeypatch):
    """Simula l'assenza di 'aiomysql' e verifica che l'import di core.config non fallisca."""
    # Force ImportError when trying to import aiomysql
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "aiomysql":
            raise ImportError("No module named 'aiomysql'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Ensure a fresh import of core.config (remove cached module)
    if "core.config" in sys.modules:
        del sys.modules["core.config"]

    import core.config as conf

    # aiomysql should be set but None (or at least import didn't raise)
    assert hasattr(conf, "aiomysql")
    assert conf.aiomysql is None


def test_list_available_cortexs_uses_registry(monkeypatch):
    """Verifica che list_available_cortexs derivi i tipi di cortex dal registry."""

    class FakeRegistry:
        def __init__(self):
            self._cortex_kinds = {
                "live": {},
                "llm_provider": {},
            }
            self._engine_meta = {
                "grok": {"cortex": "live"},
                "manual": {"cortex": "llm_provider"},
            }

    # Patch the cortex registry getter used by core.config
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )

    import core.config as conf

    kinds = conf.list_available_cortexs()
    assert "llm_provider" in kinds
    assert "live" in kinds
    assert isinstance(kinds, list)


def test_webui_accent_default():
    """WEBUI accent variable is registered and defaults to #6bfefe."""
    from core.config_manager import config_registry

    val = config_registry.get_value("WEBUI_ACCENT_COLOR", "#6bfefe")
    assert isinstance(val, str)
    assert val.lower() == "#6bfefe"


@pytest.mark.asyncio
async def test_log_chat_persistence_uses_config_registry(monkeypatch):
    """Ensure set_log_chat_id_and_thread writes via config_registry.set_value."""
    from core import config as conf
    import core.config_manager as cm

    mock = AsyncMock()
    monkeypatch.setattr(cm.config_registry, "set_value", mock)

    await conf.set_log_chat_id_and_thread(
        12345, thread_id=678, interface="telegram_bot"
    )

    # Expect set_value called for each key
    assert mock.await_count >= 3
    mock.assert_any_await("LOG_CHAT_INTERFACE", "telegram_bot")
    mock.assert_any_await("LOG_CHAT_ID", "12345")
    mock.assert_any_await("LOG_CHAT_THREAD_ID", "678")


def test_get_log_chat_reads_from_config_registry(monkeypatch):
    """Ensure getters read via config_registry.get_value."""
    from core import config as conf
    import core.config_manager as cm

    monkeypatch.setattr(
        cm.config_registry,
        "get_value",
        lambda k, d=None: (
            "telegram_bot"
            if k == "LOG_CHAT_INTERFACE"
            else (
                "12345"
                if k == "LOG_CHAT_ID"
                else ("678" if k == "LOG_CHAT_THREAD_ID" else d)
            )
        ),
    )

    # Async getters
    val = asyncio.run(conf.get_log_chat_interface())
    assert val == "telegram_bot"
    val = asyncio.run(conf.get_log_chat_id())
    assert val == 12345
    val = asyncio.run(conf.get_log_chat_thread_id())
    assert val == 678


@pytest.mark.asyncio
async def test_get_active_cortex_engine_repairs_stale_base_for_scope(monkeypatch):
    from core import config as conf
    import core.config_manager as cm

    class FakeRegistry:
        def get_available_engines(self):
            return ["anthropic", "gemini_api"]

        def get_default_engine(self):
            return "anthropic"

    values = {
        "BASE_CORTEX": "gemini",
        "GRILLO_CORTEX": "Default",
    }
    set_value = AsyncMock()

    monkeypatch.setattr(
        cm.config_registry,
        "get_value",
        lambda key, default=None: values.get(key, default),
    )
    monkeypatch.setattr(cm.config_registry, "set_value", set_value)
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )

    engine = await conf.get_active_cortex_engine("grillo")

    assert engine == "anthropic"
    set_value.assert_awaited_once_with("BASE_CORTEX", "anthropic")


@pytest.mark.asyncio
async def test_get_active_cortex_engine_resets_bad_scope_override_to_base(monkeypatch):
    from core import config as conf
    import core.config_manager as cm

    class FakeRegistry:
        def get_available_engines(self):
            return ["anthropic", "gemini_api"]

        def get_default_engine(self):
            return "anthropic"

    values = {
        "BASE_CORTEX": "gemini_api",
        "GRILLO_CORTEX": "removed_engine",
    }
    set_value = AsyncMock()

    monkeypatch.setattr(
        cm.config_registry,
        "get_value",
        lambda key, default=None: values.get(key, default),
    )
    monkeypatch.setattr(cm.config_registry, "set_value", set_value)
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )

    engine = await conf.get_active_cortex_engine("grillo")

    assert engine == "gemini_api"
    set_value.assert_awaited_once_with("GRILLO_CORTEX", "Default")


@pytest.mark.asyncio
async def test_get_active_cortex_engine_keeps_pending_external_endpoint(monkeypatch):
    """A configured-but-not-yet-registered external endpoint must not be
    treated as stale -- doing so previously caused BASE_CORTEX to be
    silently and permanently overwritten with whichever built-in engine
    module happened to sort first on disk (anthropic), any time the
    endpoint hadn't (re)registered into the CortexRegistry yet."""
    from core import config as conf
    import core.config_manager as cm

    class FakeRegistry:
        def get_available_engines(self):
            return ["anthropic", "gemini_api"]

        def get_default_engine(self):
            return "anthropic"

    class FakeEndpoint:
        def engine_name(self):
            return "Venice2"

    class FakeExternalEndpointRegistry:
        async def list_endpoints(self, enabled_only=False):
            return [FakeEndpoint()]

    values = {
        "BASE_CORTEX": "Venice2",
        "GRILLO_CORTEX": "Default",
    }
    set_value = AsyncMock()

    monkeypatch.setattr(
        cm.config_registry,
        "get_value",
        lambda key, default=None: values.get(key, default),
    )
    monkeypatch.setattr(cm.config_registry, "set_value", set_value)
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )
    monkeypatch.setattr(
        "core.external_endpoints.registry.get_external_endpoint_registry",
        lambda: FakeExternalEndpointRegistry(),
    )

    engine = await conf.get_active_cortex_engine(None)

    assert engine == "Venice2"
    set_value.assert_not_awaited()
