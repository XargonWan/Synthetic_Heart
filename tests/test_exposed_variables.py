import pytest
import time

from types import SimpleNamespace

from core.config_manager import config_registry
import core.action_parser as action_parser
import importlib


@pytest.mark.asyncio
async def test_corrector_retries_dynamic_update():
    original = int(action_parser.CORRECTOR_RETRIES)

    try:
        # Establish a known baseline regardless of env/DB overrides in the test workspace.
        await config_registry.set_value("CORRECTOR_RETRIES", 2)
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
    finally:
        await config_registry.set_value("CORRECTOR_RETRIES", original)


@pytest.mark.asyncio
async def test_should_retry_respects_updated_limit():
    original = int(action_parser.CORRECTOR_RETRIES)

    # Ensure _should_retry reads the dynamic value
    msg = SimpleNamespace()
    msg.interface_path = "test_interface/1"

    try:
        # Set a small retry limit
        await config_registry.set_value("CORRECTOR_RETRIES", 2)

        # Ensure internal tracker reflects 2 attempts already made
        action_parser._retry_tracker.clear()
        action_parser._retry_tracker[msg.interface_path] = (2, time.time())

        # Should not retry because attempts == limit
        assert not action_parser._should_retry(msg)
    finally:
        action_parser._retry_tracker.clear()
        await config_registry.set_value("CORRECTOR_RETRIES", original)


def test_default_response_timeout():
    # The configuration registry default should match the new value we set in the code.
    from core.message_chain import RESPONSE_TIMEOUT

    assert int(RESPONSE_TIMEOUT) == 300


def test_system_reply_timeout_from_config():
    from core.transport_layer import _get_system_reply_timeout
    from core.config_manager import config_registry

    definition = config_registry._definitions.get("AWAIT_RESPONSE_TIMEOUT")
    if definition is None:
        config_registry.get_var("AWAIT_RESPONSE_TIMEOUT", 600)
        original = 600
    else:
        original = config_registry.get_value("AWAIT_RESPONSE_TIMEOUT", 600)

    # Set to a custom value and verify getter returns it
    import asyncio

    try:
        asyncio.run(config_registry.set_value("AWAIT_RESPONSE_TIMEOUT", 123))
        assert _get_system_reply_timeout() == 123
    finally:
        asyncio.run(config_registry.set_value("AWAIT_RESPONSE_TIMEOUT", original))
