"""
Test that LLM plugins can be hot-swapped without full app restart.
"""
# Import test stubs first to avoid import errors
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import tests  # noqa: F401 - Import to register stubs

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from core.plugin_instance import load_plugin, plugin


@pytest.mark.asyncio
async def test_llm_plugin_hotswap_from_manual_to_manual():
    """Test switching LLM plugin from manual to manual (no-op)."""
    # Start with manual
    await load_plugin("manual")
    
    # Verify it's loaded
    assert plugin is not None
    assert "manual" in plugin.__class__.__module__
    
    # Try to load the same plugin again
    await load_plugin("manual")
    
    # Should still be the same plugin
    assert plugin is not None
    assert "manual" in plugin.__class__.__module__


@pytest.mark.asyncio
async def test_llm_plugin_hotswap_cleanup():
    """Test that plugin cleanup is called during hotswap."""
    # Load manual first
    await load_plugin("manual")
    initial_plugin = plugin
    
    # Mock cleanup method to track calls
    initial_plugin.cleanup = Mock()
    
    # Now trigger a hotswap by loading a different plugin
    with patch('core.cortex_registry.get_cortex_registry') as mock_registry:
        mock_registry_instance = Mock()
        mock_new_plugin = Mock()
        mock_new_plugin.__class__.__module__ = "cortex.llm_engine.selenium_chatgpt"
        mock_new_plugin.start = AsyncMock()
        
        # Make load_engine return a different plugin class
        mock_registry_instance.load_engine = Mock(return_value=mock_new_plugin)
        mock_registry.return_value = mock_registry_instance
        
        await load_plugin("selenium_chatgpt")
    
    # Cleanup should have been called
    initial_plugin.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_llm_plugin_worker_task_waiting():
    """Test that hotswap waits for worker task completion."""
    # Load manual first
    await load_plugin("manual")
    initial_plugin = plugin
    
    # Create a mock worker task that takes time
    mock_task = AsyncMock()
    mock_task.done.return_value = False  # Task not done initially
    mock_task.cancel = Mock()
    
    # Set a fake worker task
    initial_plugin._worker_task = mock_task
    initial_plugin.cleanup = Mock()
    
    # Simulate task completion after cancel
    async def task_completion():
        await asyncio.sleep(0.1)
    
    mock_task.side_effect = task_completion
    
    with patch('core.plugin_instance.get_cortex_registry') as mock_registry:
        mock_registry_instance = Mock()
        mock_new_plugin = Mock()
        mock_new_plugin.__class__.__module__ = "cortex.llm_engine.selenium_chatgpt"
        mock_new_plugin.start = AsyncMock()
        
        mock_registry_instance.load_engine = Mock(return_value=mock_new_plugin)
        mock_registry.return_value = mock_registry_instance
        
        await load_plugin("selenium_chatgpt")
    
    # Cleanup should have been called after waiting
    initial_plugin.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_hotswap_raises_if_start_fails_when_ensured():
    """When ensuring start during hot-swap, failures in start() should propagate."""
    # Do not rely on initial 'manual' plugin presence to avoid DB/import side-effects
    with patch('core.plugin_instance.get_cortex_registry') as mock_registry:
        mock_registry_instance = Mock()
        # Plugin whose start() raises
        async def failing_start():
            raise Exception("startboom")

        mock_new_plugin = Mock()
        mock_new_plugin.__class__.__module__ = "cortex.llm_engine.selenium_chatgpt"
        mock_new_plugin.start = failing_start

        mock_registry_instance.load_engine = Mock(return_value=mock_new_plugin)
        mock_registry.return_value = mock_registry_instance

        with pytest.raises(Exception):
            # ensure_started=True should await start and propagate
            await load_plugin("selenium_chatgpt", ensure_started=True, start_timeout=1.0)
