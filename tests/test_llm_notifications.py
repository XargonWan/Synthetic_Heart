import pytest
from unittest.mock import patch, AsyncMock, Mock
import asyncio


@pytest.fixture(autouse=True)
def ensure_default_cortexs():
    """Ensure CortexRegistry reports a minimal set of engines for tests that
    rely on get_active_cortex_engine() without performing full discovery.
    """
    from core.cortex_registry import get_cortex_registry

    reg = get_cortex_registry()
    # Keep existing registrations but add minimal fallbacks if missing
    reg._engine_meta.setdefault("manual", {"cortex": "llm_provider"})
    reg._engine_modules.setdefault("manual", "cortex.llm_provider.manual")
    reg._engine_meta.setdefault("selenium_chatgpt", {"cortex": "selenium_engine"})
    reg._engine_modules.setdefault(
        "selenium_chatgpt", "cortex.selenium_engine.selenium_chatgpt"
    )
    yield


@pytest.mark.asyncio
async def test_switch_active_cortex_notifies_on_success():
    from core.config import switch_active_cortex_engine

    mock_registry = Mock()
    mock_registry.get_available_engines = Mock(return_value=["manual"])

    with (
        patch("core.config.set_base_cortex", new=AsyncMock()) as mock_set_active,
        patch("core.plugin_instance.load_plugin", new=AsyncMock()) as mock_load_plugin,
        patch("core.cortex_registry.get_cortex_registry", return_value=mock_registry),
        patch("core.notifier.notify_trainer") as mock_notify,
    ):
        await switch_active_cortex_engine("manual", use_hot_swap=True)

        # Ensure we attempted to persist change and load plugin
        mock_set_active.assert_awaited()
        mock_load_plugin.assert_awaited()

        # Notification should have been sent to trainer
        mock_notify.assert_called_once()
        args = mock_notify.call_args[0]
        assert "Cortex engine dynamically updated" in args[0]


@pytest.mark.asyncio
async def test_switch_active_cortex_notifies_on_failure():
    from core.config import switch_active_cortex_engine

    mock_registry = Mock()
    mock_registry.get_available_engines = Mock(return_value=["manual"])

    with (
        patch("core.config.set_base_cortex", new=AsyncMock()) as mock_set_active,
        patch(
            "core.plugin_instance.load_plugin",
            new=AsyncMock(side_effect=Exception("boom")),
        ) as mock_load_plugin,
        patch("core.cortex_registry.get_cortex_registry", return_value=mock_registry),
        patch("core.notifier.notify_trainer") as mock_notify,
    ):
        # Kick off two concurrent switches to exercise the lock; both should
        # propagate the error from the plugin loader.
        task1 = asyncio.create_task(
            switch_active_cortex_engine("manual", use_hot_swap=True)
        )
        task2 = asyncio.create_task(
            switch_active_cortex_engine("manual", use_hot_swap=True)
        )

        with pytest.raises(Exception):
            await task1
        with pytest.raises(Exception):
            await task2

        # Notification should have been attempted for failure
        assert mock_notify.call_count >= 1
        args = mock_notify.call_args[0]
        assert "Failed to switch Cortex" in args[0] or "Error" in args[0]


@pytest.mark.asyncio
async def test_cortex_command_uses_switch_active_cortex():
    from core import command_registry

    with patch(
        "core.config.switch_active_cortex_engine", new=AsyncMock()
    ) as mock_switch:
        res = await command_registry.cortex_command("manual")
        mock_switch.assert_awaited_with("manual", use_hot_swap=False)
        assert "Cortex engine dynamically updated" in res


@pytest.mark.asyncio
async def test_cortex_command_lists_engines():
    """`/cortex` with no args should list kinds and engines and show active engine."""
    from core import command_registry

    with (
        patch(
            "core.config.get_active_cortex_engine", new=AsyncMock(return_value="manual")
        ),
        patch(
            "core.config.list_available_cortexs",
            return_value=["llm_provider", "selenium_engine"],
        ),
        patch("core.config.list_available_cortex_engines") as mock_list_engines,
    ):

        def _list(kind=None):
            if kind == "llm_provider":
                return ["manual", "gpt"]
            if kind == "selenium_engine":
                return ["selenium_gemini"]
            return ["manual", "gpt", "selenium_gemini"]

        mock_list_engines.side_effect = _list
        res = await command_registry.cortex_command()
        assert "*Active Cortex:* `manual`" in res
        assert "llm_provider:" in res
        assert "selenium_engine:" in res
        assert "`llm_provider/manual`" in res
        assert "`selenium_engine/selenium_gemini`" in res


@pytest.mark.asyncio
async def test_cortex_command_fqdn_sets_engine():
    """Fully-qualified `/cortex kind/engine` should set the engine by short name."""
    from core import command_registry

    mock_reg = Mock()
    mock_reg._engine_meta = {}
    mock_reg.get_available_engines = Mock(return_value=["manual"])

    with (
        patch(
            "core.config.get_active_cortex_engine", new=AsyncMock(return_value="manual")
        ),
        patch("core.config.list_available_cortexs", return_value=["llm_provider"]),
        patch("core.config.list_available_cortex_engines", return_value=["manual"]),
        patch("core.cortex_registry.get_cortex_registry", return_value=mock_reg),
        patch(
            "core.config.switch_active_cortex_engine", new=AsyncMock()
        ) as mock_switch,
    ):
        res = await command_registry.cortex_command("llm_provider/manual")
        mock_switch.assert_awaited_with("manual", use_hot_swap=False)
        assert "dynamically updated to `manual`" in res


@pytest.mark.asyncio
async def test_cortex_command_ambiguous_shortname():
    """When a short-name matches multiple engines, /cortex should ask for disambiguation."""
    from core import command_registry

    mock_reg = Mock()
    mock_reg._engine_meta = {
        "gemini_live": {"cortex": "llm_provider"},
        "selenium_gemini": {"cortex": "selenium_engine"},
    }
    mock_reg.get_available_engines = Mock(
        return_value=["gemini_live", "selenium_gemini"]
    )

    with (
        patch("core.cortex_registry.get_cortex_registry", return_value=mock_reg),
        patch(
            "core.config.get_active_cortex_engine", new=AsyncMock(return_value="manual")
        ),
        patch(
            "core.config.list_available_cortexs",
            return_value=["llm_provider", "selenium_engine"],
        ),
        patch(
            "core.config.list_available_cortex_engines",
            return_value=["gemini_live", "selenium_gemini"],
        ),
    ):
        res = await command_registry.cortex_command("gemini")
        assert "Found multiple matching engines for 'gemini'" in res
        assert "/cortex llm_provider/gemini_live" in res
        assert "/cortex selenium_engine/selenium_gemini" in res


@pytest.mark.asyncio
async def test_llm_alias_deprecation_prefix():
    from core import command_registry

    with patch("core.config.switch_active_cortex_engine", new=AsyncMock()):
        res = await command_registry.llm_alias("manual")
        assert res.startswith("⚠️ `/llm` is deprecated")
        assert "dynamically updated to `manual`" in res


@pytest.mark.asyncio
async def test_switch_active_cortex_notifies_on_start_failure(monkeypatch):
    """If plugin.start() raises during a hot-swap (ensure_started), the failure should be propagated and trainer notified."""
    from core.config import switch_active_cortex_engine

    # Ensure set_base_cortex succeeds
    monkeypatch.setattr("core.config.set_base_cortex", AsyncMock())

    # Patch plugin registry used by plugin_instance to return a plugin whose start() raises
    async def failing_start():
        raise Exception("start-failed")

    mock_registry = Mock()
    mock_registry.get_available_engines = Mock(return_value=["selenium_chatgpt"])
    mock_plugin = Mock()
    mock_plugin.__class__.__module__ = "cortex.selenium_engine.selenium_chatgpt"
    mock_plugin.start = failing_start
    mock_registry.load_engine = Mock(return_value=mock_plugin)

    # Patch registry in both the cortex_registry module and in core.plugin_instance
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", Mock(return_value=mock_registry)
    )
    monkeypatch.setattr(
        "core.plugin_instance.get_cortex_registry", Mock(return_value=mock_registry)
    )

    with patch("core.notifier.notify_trainer") as mock_notify:
        with pytest.raises(Exception):
            await switch_active_cortex_engine("selenium_chatgpt", use_hot_swap=True)

        mock_notify.assert_called()
        args = mock_notify.call_args[0]
        assert "Failed to switch Cortex" in args[0]


@pytest.mark.asyncio
async def test_switch_active_cortex_reloads_when_config_matches_but_plugin_differs(
    monkeypatch,
):
    from core.config import switch_active_cortex_engine
    import core.plugin_instance as plugin_instance

    # Ensure isolation from other tests that may have loaded a plugin
    original_plugin = plugin_instance.plugin
    plugin_instance.plugin = type(
        "Dummy", (), {"__module__": "cortex.llm_provider.other"}
    )()

    # BASE_CORTEX already set to 'manual', but loaded plugin is 'other'
    async def fake_get_active_cortex_engine():
        return "manual"

    monkeypatch.setattr(
        "core.config.get_active_cortex_engine", fake_get_active_cortex_engine
    )
    mock_registry = Mock()
    mock_registry.get_available_engines = Mock(return_value=["manual", "other"])
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: mock_registry
    )
    monkeypatch.setattr(
        "core.config.list_available_cortex_engines",
        lambda *_args, **_kwargs: ["manual", "other"],
    )

    called = {"count": 0}

    async def fake_load_plugin(name, **kwargs):
        called["count"] += 1
        assert name == "manual"
        # emulate successful load
        plugin_instance.plugin = type(
            "Dummy", (), {"__module__": "cortex.llm_provider.manual"}
        )()

    monkeypatch.setattr("core.config.set_base_cortex", AsyncMock())
    monkeypatch.setattr("core.plugin_instance.load_plugin", fake_load_plugin)

    try:
        await switch_active_cortex_engine("manual", use_hot_swap=True)
        assert called["count"] == 1
        assert plugin_instance.plugin.__class__.__module__.endswith("manual")
    finally:
        plugin_instance.plugin = original_plugin


@pytest.mark.asyncio
async def test_load_plugin_ensures_start_propagates(monkeypatch):
    """Directly calling load_plugin with ensure_started should propagate start() errors."""
    from core.plugin_instance import load_plugin

    async def failing_start():
        raise Exception("startboom")

    mock_registry = Mock()
    mock_registry.get_available_engines = Mock(return_value=["selenium_chatgpt"])
    mock_plugin = Mock()
    mock_plugin.__class__.__module__ = "cortex.selenium_engine.selenium_chatgpt"
    mock_plugin.start = failing_start
    mock_registry.load_engine = Mock(return_value=mock_plugin)

    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", Mock(return_value=mock_registry)
    )
    monkeypatch.setattr(
        "core.plugin_instance.get_cortex_registry", Mock(return_value=mock_registry)
    )

    # Ensure global plugin is reset to avoid interference from other tests
    import core.plugin_instance as plugin_module

    plugin_module.plugin = None

    with pytest.raises(Exception):
        await load_plugin("selenium_chatgpt", ensure_started=True, start_timeout=1.0)
