"""Test that Cortex plugins can be hot-swapped without full app restart."""

# Import test stubs first to avoid import errors
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import tests  # noqa: F401 - Import to register stubs

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
import core.plugin_instance as plugin_instance_module
from core.plugin_instance import load_plugin, get_plugin


class _FakeManualPlugin:
    __module__ = "cortex.llm_provider.manual"

    def __init__(self):
        self.cleanup = Mock()


class _FakeSeleniumPlugin:
    __module__ = "cortex.selenium_engine.selenium_chatgpt"

    def __init__(self):
        self.start = AsyncMock()


class _FakeRegistry:
    def __init__(self, manual_plugin=None, selenium_plugin=None):
        self._engines = {
            "manual": manual_plugin or _FakeManualPlugin(),
            "selenium_chatgpt": selenium_plugin or _FakeSeleniumPlugin(),
        }

    def get_engine(self, name):
        return self._engines.get(name)

    def load_engine(self, name, notify_fn=None):
        return self._engines[name]


@pytest.fixture(autouse=True)
def reset_loaded_plugin():
    original = plugin_instance_module.plugin
    plugin_instance_module.plugin = None
    try:
        yield
    finally:
        plugin_instance_module.plugin = None
        plugin_instance_module.plugin = original


@pytest.mark.asyncio
async def test_cortex_plugin_hotswap_from_manual_to_manual():
    """Test switching Cortex plugin from manual to manual (no-op)."""
    registry = _FakeRegistry()

    with patch("core.plugin_instance.get_cortex_registry", return_value=registry):
        # Start with manual
        await load_plugin("manual")
        plugin = get_plugin()

        # Verify it's loaded
        assert plugin is not None
        assert "manual" in plugin.__class__.__module__

        # Try to load the same plugin again
        await load_plugin("manual")
        plugin = get_plugin()

        # Should still be the same plugin
        assert plugin is registry.get_engine("manual")
        assert "manual" in plugin.__class__.__module__


@pytest.mark.asyncio
async def test_cortex_plugin_hotswap_cleanup():
    """Test that plugin cleanup is called during hotswap."""
    manual_plugin = _FakeManualPlugin()
    selenium_plugin = _FakeSeleniumPlugin()
    registry = _FakeRegistry(
        manual_plugin=manual_plugin, selenium_plugin=selenium_plugin
    )

    with patch("core.plugin_instance.get_cortex_registry", return_value=registry):
        # Load manual first
        await load_plugin("manual")
        initial_plugin = get_plugin()

        # Now trigger a hotswap by loading a different plugin
        await load_plugin("selenium_chatgpt")

        # Cleanup should have been called
        initial_plugin.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_cortex_plugin_worker_task_waiting():
    """Test that hotswap waits for worker task completion."""
    manual_plugin = _FakeManualPlugin()
    selenium_plugin = _FakeSeleniumPlugin()
    registry = _FakeRegistry(
        manual_plugin=manual_plugin, selenium_plugin=selenium_plugin
    )

    with patch("core.plugin_instance.get_cortex_registry", return_value=registry):
        # Load manual first
        await load_plugin("manual")
        initial_plugin = get_plugin()

        # Create a mock worker task that takes time
        mock_task = AsyncMock()
        mock_task.done.return_value = False
        mock_task.cancel = Mock()

        # Set a fake worker task
        initial_plugin._worker_task = mock_task
        initial_plugin.cleanup = Mock()

        # Simulate task completion after cancel
        async def task_completion():
            await asyncio.sleep(0.1)

        mock_task.side_effect = task_completion

        await load_plugin("selenium_chatgpt")

        # Cleanup should have been called after waiting
        initial_plugin.cleanup.assert_called_once()

        # We must NOT force-cancel an ongoing worker task on hotswap timeouts —
        # rely on the engine's own waiting logic instead (Selenium handles streaming)
        mock_task.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_hotswap_raises_if_start_fails_when_ensured():
    """When ensuring start during hot-swap, failures in start() should propagate."""
    # Do not rely on initial 'manual' plugin presence to avoid DB/import side-effects
    with patch("core.plugin_instance.get_cortex_registry") as mock_registry:
        mock_registry_instance = Mock()

        # Plugin whose start() raises
        async def failing_start():
            raise Exception("startboom")

        mock_new_plugin = Mock()
        mock_new_plugin.__class__.__module__ = "cortex.selenium_engine.selenium_chatgpt"
        mock_new_plugin.start = failing_start

        mock_registry_instance.load_engine = Mock(return_value=mock_new_plugin)
        mock_registry.return_value = mock_registry_instance

        with pytest.raises(Exception):
            # ensure_started=True should await start and propagate
            await load_plugin(
                "selenium_chatgpt", ensure_started=True, start_timeout=1.0
            )
