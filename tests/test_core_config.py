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
    """set_log_chat_id_and_thread writes a single interface_path to LOG_CHAT_ID."""
    from core import config as conf
    import core.config_manager as cm

    conf._log_chat_path = None
    mock = AsyncMock()
    monkeypatch.setattr(cm.config_registry, "set_value", mock)

    await conf.set_log_chat_id_and_thread(
        12345, thread_id=678, interface="telegram_bot"
    )

    # Expect a single write with the composed interface_path.
    mock.assert_awaited_once_with("LOG_CHAT_ID", "telegram_bot/12345/678")


@pytest.mark.asyncio
async def test_log_chat_persistence_omits_empty_thread(monkeypatch):
    """A None thread_id produces a two-segment interface_path."""
    from core import config as conf
    import core.config_manager as cm

    conf._log_chat_path = None
    mock = AsyncMock()
    monkeypatch.setattr(cm.config_registry, "set_value", mock)

    await conf.set_log_chat_id_and_thread(12345, interface="telegram_bot")

    mock.assert_awaited_once_with("LOG_CHAT_ID", "telegram_bot/12345")


def test_get_log_chat_reads_from_config_registry(monkeypatch):
    """Getters derive interface/chat/thread from the single LOG_CHAT_ID path."""
    from core import config as conf
    import core.config_manager as cm

    conf._log_chat_path = None
    monkeypatch.setattr(
        cm.config_registry,
        "get_value",
        lambda k, d=None: "telegram_bot/12345/678" if k == "LOG_CHAT_ID" else d,
    )

    # Async getters
    val = asyncio.run(conf.get_log_chat_interface())
    assert val == "telegram_bot"
    val = asyncio.run(conf.get_log_chat_id())
    assert val == 12345
    val = asyncio.run(conf.get_log_chat_thread_id())
    assert val == 678

    # Sync getters share the same derivation.
    assert conf.get_log_chat_interface_sync() == "telegram_bot"
    assert conf.get_log_chat_id_sync() == 12345
    assert conf.get_log_chat_thread_id_sync() == 678


def test_log_chat_listener_invalidates_cache(monkeypatch):
    """A direct LOG_CHAT_ID change (e.g. WebUI) refreshes the cached path."""
    from core import config as conf
    import core.config_manager as cm

    # An empty registry so a cleared cache never reloads a real value.
    monkeypatch.setattr(cm.config_registry, "get_value", lambda k, d=None: d)

    # Seed the cache with an initial value.
    conf._log_chat_path = "telegram_bot/111"
    assert conf.get_log_chat_id_sync() == 111

    # Simulate the config_registry listener firing on a new value.
    conf._on_log_chat_id_changed("discord_bot/222/9")
    assert conf.get_log_chat_interface_sync() == "discord_bot"
    assert conf.get_log_chat_id_sync() == 222
    assert conf.get_log_chat_thread_id_sync() == 9

    # An empty value clears the cache.
    conf._on_log_chat_id_changed("")
    assert conf.get_log_chat_id_sync() is None


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
        capabilities = {"cortex": True}

        def engine_name(self):
            return "Venice2"

        def effective_subsystem_map(self):
            return {"cortex": True}

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


@pytest.mark.asyncio
async def test_get_active_cortex_engine_override_to_noncortex_endpoint_falls_back_to_base(
    monkeypatch,
):
    """General rule: a scope override pointing at a configured external
    endpoint that does NOT advertise the cortex capability must degrade
    transparently to the non-override Base Cortex, and the override key must
    be reset to 'Default'. This is the AGENT_CORTEX=logfare-mykey (cortex:false)
    401 case -- keeping a non-cortex endpoint would starve the scope."""
    from core import config as conf
    import core.config_manager as cm

    class FakeRegistry:
        def get_available_engines(self):
            return ["selenium-llm-engine"]

        def get_default_engine(self):
            return "selenium-llm-engine"

    class FakeEndpoint:
        # Auto-probe found no cortex capability (the honest signal the resolver
        # keys off), even though this endpoint may still be reachable.
        capabilities = {"cortex": False}

        def engine_name(self):
            return "logfare-mykey"

        def effective_subsystem_map(self):
            return {"cortex": False}

    class FakeExternalEndpointRegistry:
        async def list_endpoints(self, enabled_only=False):
            return [FakeEndpoint()]

    values = {
        "BASE_CORTEX": "selenium-llm-engine",
        "AGENT_CORTEX": "logfare-mykey",
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
    # Silence LogChat delivery in the unit test.
    monkeypatch.setattr("core.notifier.notifier", lambda *a, **k: None)
    conf._CORTEX_OVERRIDE_FALLBACK_WARNED.clear()

    engine = await conf.get_active_cortex_engine("agent")

    assert engine == "selenium-llm-engine"
    set_value.assert_any_await("AGENT_CORTEX", "Default")


@pytest.mark.asyncio
async def test_get_active_cortex_engine_avoids_keyless_anthropic_fallback(
    monkeypatch,
):
    """BASE_CORTEX stuck at 'anthropic' with no ANTHROPIC_API_KEY configured
    must self-heal to a sibling scope's already-working engine (e.g. Venice
    from TRAINER_CORTEX) instead of silently returning 'anthropic' again --
    anthropic is a real registered built-in so the plain staleness check
    never fires for it, but without a key it doesn't raise, it returns a
    fixed 'not configured' string that loops the JSON corrector forever
    (see FIXED_ISSUES.md)."""
    from core import config as conf
    import core.config_manager as cm

    class FakeRegistry:
        def get_available_engines(self):
            return ["anthropic", "gemini_api", "Venice"]

        def get_default_engine(self):
            return "anthropic"

    values = {
        "BASE_CORTEX": "anthropic",
        "TRAINER_CORTEX": "Venice",
        "GRILLO_CORTEX": "Default",
        "ANTHROPIC_API_KEY": "",
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

    engine = await conf.get_active_cortex_engine(None)

    assert engine == "Venice"
    set_value.assert_awaited_once_with("BASE_CORTEX", "Venice")


@pytest.mark.asyncio
async def test_get_active_cortex_engine_allows_anthropic_when_key_configured(
    monkeypatch,
):
    """A deliberately-configured anthropic with a real key must not be
    treated as unavailable -- the keyless guard is opt-in based on whether
    ANTHROPIC_API_KEY is actually set."""
    from core import config as conf
    import core.config_manager as cm

    class FakeRegistry:
        def get_available_engines(self):
            return ["anthropic", "gemini_api"]

        def get_default_engine(self):
            return "anthropic"

    values = {
        "BASE_CORTEX": "anthropic",
        "GRILLO_CORTEX": "Default",
        "ANTHROPIC_API_KEY": "sk-ant-real-key",
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

    engine = await conf.get_active_cortex_engine(None)

    assert engine == "anthropic"
    set_value.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_active_cortex_engine_registered_noncortex_endpoint_falls_back(
    monkeypatch,
):
    """Primary-path gap: an override engine that IS registered (and whose probe
    even read 'success') but whose external endpoint advertises cortex=False must
    still degrade to Base and reset the override to 'Default'. This is the exact
    live AGENT_CORTEX=logfare-mykey case -- it passed the plain `chosen in
    available` check and was returned verbatim, 401ing every turn. Unlike the
    sibling test above, here `logfare-mykey` IS in get_available_engines()."""
    from core import config as conf
    import core.config_manager as cm

    class FakeRegistry:
        def get_available_engines(self):
            # logfare-mykey IS registered here -- the primary-path gap.
            return ["selenium-llm-engine", "logfare-mykey"]

        def get_default_engine(self):
            return "selenium-llm-engine"

    class FakeEndpoint:
        # logfare-mykey's auto-probe found no cortex capability. A manual
        # subsystem_map override forcing cortex=true must NOT rescue it -- the
        # resolver keys off the probed capabilities, so effective_subsystem_map
        # returning True here would be a trap the fix must ignore.
        capabilities = {"cortex": False}

        def engine_name(self):
            return "logfare-mykey"

        def effective_subsystem_map(self):
            # Simulate the live misconfiguration: operator override says cortex.
            return {"cortex": True}

    class FakeExternalEndpointRegistry:
        async def list_endpoints(self, enabled_only=False):
            return [FakeEndpoint()]

    values = {
        "BASE_CORTEX": "selenium-llm-engine",
        "AGENT_CORTEX": "logfare-mykey",
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
    monkeypatch.setattr("core.notifier.notifier", lambda *a, **k: None)
    conf._CORTEX_OVERRIDE_FALLBACK_WARNED.clear()

    engine = await conf.get_active_cortex_engine("agent")

    assert engine == "selenium-llm-engine"
    set_value.assert_any_await("AGENT_CORTEX", "Default")
