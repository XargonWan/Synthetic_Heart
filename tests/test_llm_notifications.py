import pytest
from unittest.mock import patch, AsyncMock, Mock


@pytest.mark.asyncio
async def test_switch_active_llm_notifies_on_success():
    from core.config import switch_active_llm

    with patch("core.config.set_active_llm", new=AsyncMock()) as mock_set_active, \
         patch("core.plugin_instance.load_plugin", new=AsyncMock()) as mock_load_plugin, \
         patch("core.notifier.notify_trainer") as mock_notify:

        await switch_active_llm("manual", use_hot_swap=True)

        # Ensure we attempted to persist change and load plugin
        mock_set_active.assert_awaited()
        mock_load_plugin.assert_awaited()

        # Notification should have been sent to trainer
        mock_notify.assert_called_once()
        args = mock_notify.call_args[0]
        assert "LLM mode dynamically updated" in args[0]


@pytest.mark.asyncio
async def test_switch_active_llm_notifies_on_failure():
    from core.config import switch_active_llm

    with patch("core.config.set_active_llm", new=AsyncMock()) as mock_set_active, \
         patch("core.plugin_instance.load_plugin", new=AsyncMock(side_effect=Exception("boom"))) as mock_load_plugin, \
         patch("core.notifier.notify_trainer") as mock_notify:

        with pytest.raises(Exception):
            await switch_active_llm("manual", use_hot_swap=True)

        # Notification should have been attempted for failure
        mock_notify.assert_called_once()
        args = mock_notify.call_args[0]
        assert "Failed to switch LLM" in args[0] or "Error" in args[0]


@pytest.mark.asyncio
async def test_llm_command_uses_switch_active_llm():
    from core import command_registry

    with patch("core.config.switch_active_llm", new=AsyncMock()) as mock_switch:
        res = await command_registry.llm_command("manual")
        mock_switch.assert_awaited_with("manual", use_hot_swap=False)
        assert "LLM mode dynamically updated" in res
