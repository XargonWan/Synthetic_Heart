import pytest
import time

from types import SimpleNamespace

from core.config_manager import config_registry
import core.action_parser as action_parser
import importlib


@pytest.mark.asyncio
async def test_corrector_retries_dynamic_update():
    # Default should be 2 as defined in registration
    assert int(action_parser.CORRECTOR_RETRIES) == 2

    # Update via config_registry and ensure consumers see the new value
    await config_registry.set_value("CORRECTOR_RETRIES", 5)
    assert int(action_parser.CORRECTOR_RETRIES) == 5

    # Try importing plugin that references the value; skip if deps missing
    try:
        event_plugin = importlib.import_module("plugins.event_plugin")
    except Exception:
        pytest.skip("plugins.event_plugin not importable in test environment")
    assert int(event_plugin.CORRECTOR_RETRIES) == 5


@pytest.mark.asyncio
async def test_should_retry_respects_updated_limit():
    # Ensure _should_retry reads the dynamic value
    msg = SimpleNamespace()
    msg.interface_path = "test_interface/1"

    # Set a small retry limit
    await config_registry.set_value("CORRECTOR_RETRIES", 2)

    # Ensure internal tracker reflects 2 attempts already made
    action_parser._retry_tracker.clear()
    action_parser._retry_tracker[msg.interface_path] = (2, time.time())

    # Should not retry because attempts == limit
    assert not action_parser._should_retry(msg)


def test_chromium_headless_reflects_config_change():
    # Instantiate a SeleniumLLMBase-derived object minimally to test listener
    from cortex.selenium_engine.selenium_llm_base import SeleniumLLMBase
    from core.config_manager import config_registry

    inst = SeleniumLLMBase(notify_fn=None, config={})

    # Default should be False (0)
    assert inst.CHROMIUM_HEADLESS in (False, 0)

    # Update config and verify instance flag updates
    import asyncio

    asyncio.run(config_registry.set_value("CHROMIUM_HEADLESS", 1))
    assert inst.CHROMIUM_HEADLESS is True


def test_system_reply_timeout_from_config():
    from core.transport_layer import _get_system_reply_timeout
    from core.config_manager import config_registry

    # Set to a custom value and verify getter returns it
    import asyncio

    asyncio.run(config_registry.set_value("AWAIT_RESPONSE_TIMEOUT", 123))
    assert _get_system_reply_timeout() == 123
