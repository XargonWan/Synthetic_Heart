import asyncio
from datetime import datetime

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import core.plugin_instance as plugin_instance
from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
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


def test_plugin_instance_forwards_prompt_request_to_external_bridge(monkeypatch):
    prebuilt = {
        "input": {
            "payload": {
                "text": "task run",
                "source": {"interface_path": "agent:task"},
            }
        },
        "system_message": {"type": "agent_iteration"},
    }

    endpoint = ExternalEndpoint(
        id=1,
        name="my_ep",
        display_label="My EP",
        protocol=EndpointProtocol.OPENAI,
        base_url="http://localhost:11435",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={"cortex": True},
        available_models=["model-a"],
        default_model="model-a",
        probe_status="success",
        last_probe_at=None,
        extra_config={},
    )
    adapter_mock = MagicMock()
    adapter_mock.chat_completion = AsyncMock(return_value=MagicMock(content="ok"))
    bridge = ExternalCortexEngine(endpoint, adapter_mock)

    _route_to_fake_plugin(monkeypatch, bridge)

    asyncio.run(
        plugin_instance.handle_incoming_message(
            bot=None, message=None, context_memory_or_prompt=prebuilt
        )
    )

    await_args = adapter_mock.chat_completion.await_args
    assert await_args is not None
    sent_messages = await_args.args[0]
    assert sent_messages[-1]["role"] == "user"
    assert "task run" in str(sent_messages[-1]["content"])
    assert "input" not in str(sent_messages[-1]["content"])


def test_plugin_instance_updates_grillo_log_for_empty_response(monkeypatch):
    prebuilt = {
        "input": {
            "payload": {
                "text": "task run",
                "source": {"interface_path": "agent:task"},
            }
        },
        "system_message": {"type": "agent_iteration"},
        "activity_log_id": 42,
        "grillo_beat": True,
    }

    async def fake_handle_incoming_message(bot, message, prompt):
        return ""

    fake_plugin = SimpleNamespace(
        handle_incoming_message=fake_handle_incoming_message,
        model_limits_map={"default": 1000},
        _last_response_metadata={
            "finish_reason": "safety",
            "block_reason": "PROHIBITED_CONTENT",
        },
    )
    _route_to_fake_plugin(monkeypatch, fake_plugin)

    update_mock = AsyncMock()
    monkeypatch.setattr(plugin_instance, "_update_grillo_response", update_mock)

    asyncio.run(
        plugin_instance.handle_incoming_message(
            bot=None, message=None, context_memory_or_prompt=prebuilt
        )
    )

    update_mock.assert_awaited_once()
    await_call = update_mock.await_args
    assert await_call is not None
    assert await_call.args[0] == 42
    assert await_call.args[1] == ""
    assert await_call.kwargs["response_metadata"] == {
        "finish_reason": "safety",
        "block_reason": "PROHIBITED_CONTENT",
    }
