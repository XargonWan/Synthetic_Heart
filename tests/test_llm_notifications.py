import pytest
from unittest.mock import patch, AsyncMock, Mock
import asyncio


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

    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", Mock(return_value=mock_registry)
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

    monkeypatch.setattr("core.config.set_base_cortex", lambda *_args, **_kwargs: None)
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

    # Ensure global plugin is reset to avoid interference from other tests
    import core.plugin_instance as plugin_module

    plugin_module.plugin = None

    with pytest.raises(Exception):
        await load_plugin("selenium_chatgpt", ensure_started=True, start_timeout=1.0)
