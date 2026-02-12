import asyncio

from types import SimpleNamespace

import core.plugin_instance as plugin_instance


async def fake_get_local_time_fields(dt, interface_path):
    return {"local_time": "22:45", "local_hour": 22, "time_of_day": "late_evening", "local_date": "2026-02-10"}


def test_plugin_instance_injects_local_time_for_prebuilt_prompt(monkeypatch):
    # Prepare a pre-built prompt (simulating agent or other prebuilt prompt)
    prebuilt = {
        "input": {"payload": {"text": "task run", "source": {"interface_path": "agent:task"}}},
        "system_message": {"type": "agent_iteration"},
    }

    # Ensure plugin exists and captures the prompt argument
    captured = {}

    async def fake_handle_incoming_message(bot, message, prompt):
        captured['prompt'] = prompt
        # Return None to avoid passing through message_chain
        return None

    fake_plugin = SimpleNamespace(handle_incoming_message=fake_handle_incoming_message)
    monkeypatch.setattr(plugin_instance, "plugin", fake_plugin)

    # Patch get_local_time_fields to deterministic value
    monkeypatch.setattr("core.time_zone_utils.get_local_time_fields", fake_get_local_time_fields)

    # Call handle_incoming_message with prebuilt prompt (message None path)
    asyncio.run(plugin_instance.handle_incoming_message(bot=None, message=None, context_memory_or_prompt=prebuilt))

    # Assert that plugin received a prompt with local_time fields injected
    prompt_sent = captured.get('prompt')
    assert prompt_sent is not None
    payload = prompt_sent.get('input', {}).get('payload', {})
    assert payload.get('local_time') == "22:45"
    assert payload.get('local_hour') == 22
    assert payload.get('time_of_day') == "late_evening"
