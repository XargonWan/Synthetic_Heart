import asyncio
from datetime import datetime

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import core.plugin_instance as plugin_instance
from core.prompt_request import PromptRequest


async def _dummy_gather(message, ctx):
    return {}


def fake_utc_to_local(dt):
    return datetime(2026, 2, 10, 22, 45, tzinfo=ZoneInfo("Europe/Rome"))


def _route_to_fake_plugin(monkeypatch, fake_plugin):
    async def fake_get_active_cortex_engine(scope=None):
        return "fake"

    class _FakeRegistry:
        def __init__(self, engine):
            self._engine = engine
            self._engines = {"fake": engine}

        def get_engine(self, name):
            if name == "fake":
                return self._engine
            return None

        def load_engine(self, name, notify_fn=None):
            if name == "fake":
                return self._engine
            return None

    monkeypatch.setattr(plugin_instance, "plugin", fake_plugin)
    monkeypatch.setattr(
        "core.config.get_active_cortex_engine", fake_get_active_cortex_engine
    )
    monkeypatch.setattr(
        plugin_instance, "get_cortex_registry", lambda: _FakeRegistry(fake_plugin)
    )


def test_plugin_instance_injects_local_time_for_prebuilt_prompt(monkeypatch):
    # Prepare a pre-built prompt (simulating agent or other prebuilt prompt)
    prebuilt = {
        "input": {
            "payload": {"text": "task run", "source": {"interface_path": "agent:task"}}
        },
        "system_message": {"type": "agent_iteration"},
    }

    # Ensure plugin exists and captures the prompt argument
    captured = {}

    async def fake_handle_incoming_message(bot, message, prompt):
        captured["prompt"] = prompt
        # Return None to avoid passing through message_chain
        return None

    fake_plugin = SimpleNamespace(
        handle_incoming_message=fake_handle_incoming_message,
        model_limits_map={"default": 1000},
    )
    _route_to_fake_plugin(monkeypatch, fake_plugin)

    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)
    monkeypatch.setattr("core.time_zone_utils.utc_to_local", fake_utc_to_local)

    # Call handle_incoming_message with prebuilt prompt (message None path)
    asyncio.run(
        plugin_instance.handle_incoming_message(
            bot=None, message=None, context_memory_or_prompt=prebuilt
        )
    )

    # Assert that plugin received a prompt with local_time fields injected
    prompt_sent = captured.get("prompt")
    assert prompt_sent is not None
    payload = prompt_sent.get("input", {}).get("payload", {})
    assert payload.get("local_time") == "22:45"
    assert payload.get("local_hour") == 22
    assert payload.get("time_of_day") == "late_evening"


def test_plugin_instance_prebuilt_prompt_uses_payload_text(monkeypatch):
    prebuilt = {
        "input": {
            "payload": {
                "text": "task run",
                "source": {"interface_path": "agent:task"},
            }
        },
        "system_message": {"type": "agent_iteration"},
    }

    captured = {}

    async def fake_handle_incoming_message(bot, message, prompt):
        captured["message_text"] = getattr(message, "text", None)
        return None

    fake_plugin = SimpleNamespace(handle_incoming_message=fake_handle_incoming_message)
    _route_to_fake_plugin(monkeypatch, fake_plugin)

    asyncio.run(
        plugin_instance.handle_incoming_message(
            bot=None, message=None, context_memory_or_prompt=prebuilt
        )
    )

    assert captured.get("message_text") == "task run"


def test_plugin_instance_uses_prompt_request_for_opted_in_engines(monkeypatch):
    prebuilt = {
        "input": {
            "payload": {
                "text": "task run",
                "source": {"interface_path": "agent:task"},
            }
        },
        "system_message": {"type": "agent_iteration"},
    }

    captured = {}

    async def fake_handle_incoming_message(bot, message, prompt):
        captured["prompt"] = prompt
        return None

    fake_plugin = SimpleNamespace(
        handle_incoming_message=fake_handle_incoming_message,
        model_limits_map={"default": 1000},
        supports_prompt_request=True,
    )
    _route_to_fake_plugin(monkeypatch, fake_plugin)

    asyncio.run(
        plugin_instance.handle_incoming_message(
            bot=None, message=None, context_memory_or_prompt=prebuilt
        )
    )

    prompt_sent = captured.get("prompt")
    assert isinstance(prompt_sent, PromptRequest)
    assert prompt_sent.current_text == "task run"
