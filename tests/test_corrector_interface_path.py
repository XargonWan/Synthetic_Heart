"""Regression test: corrector example ``interface_path`` must not end in a slash.

The corrector's ``required_format`` example is built from
``{iface}/{chat_id}/{thread or ''}``. With a ``None`` thread the old code
produced a trailing slash (``telegram_bot/1/``), which a weak model copies into
its correction reply; if that message ever reached the interface it would be
stored under a DIFFERENT history key than the canonical ``telegram_bot/1`` and
the model would never see its own line again ("forgot what itself said one turn
prior"). This verifies the example path is clean when no thread exists and
includes the thread id when one does.
"""

import json

import pytest

import core.transport_layer as transport_layer


class _FakePlugin:
    def __init__(self) -> None:
        self.captured_prompt: str | None = None

    async def handle_incoming_message(self, bot, message, prompt) -> str:
        self.captured_prompt = prompt
        # Return valid JSON so the corrector loop ends on the first attempt.
        return (
            '{"actions": [{"type": "message_telegram_bot", "payload": {"text": "hi"}}]}'
        )


@pytest.mark.asyncio
async def test_corrector_example_interface_path_no_trailing_slash(monkeypatch):
    fake_plugin = _FakePlugin()

    import core.plugin_instance as plugin_instance

    monkeypatch.setattr(
        plugin_instance, "get_plugin", lambda: fake_plugin, raising=False
    )

    # No thread_id: the example path must be exactly ``telegram_bot/12345``.
    await transport_layer.run_corrector_middleware(
        text="not valid json",
        bot=None,
        context={"interface": "telegram_bot", "interface_path": "telegram_bot/12345"},
        chat_id=12345,
        thread_id=None,
    )
    prompt = fake_plugin.captured_prompt or ""
    assert "telegram_bot/12345/" not in prompt
    assert '"interface_path": "telegram_bot/12345"' in prompt

    # With a thread_id, the example path carries it and still has no trailing slash.
    fake_plugin.captured_prompt = None
    await transport_layer.run_corrector_middleware(
        text="not valid json",
        bot=None,
        context={"interface": "telegram_bot", "interface_path": "telegram_bot/12345"},
        chat_id=12345,
        thread_id=678,
    )
    prompt = fake_plugin.captured_prompt or ""
    assert '"interface_path": "telegram_bot/12345/678"' in prompt
    assert '"interface_path": "telegram_bot/12345/678/"' not in prompt


@pytest.mark.asyncio
async def test_corrector_example_interface_path_is_valid_json_field(monkeypatch):
    fake_plugin = _FakePlugin()

    import core.plugin_instance as plugin_instance

    monkeypatch.setattr(
        plugin_instance, "get_plugin", lambda: fake_plugin, raising=False
    )

    await transport_layer.run_corrector_middleware(
        text="not valid json",
        bot=None,
        context={"interface": "telegram_bot", "interface_path": "telegram_bot/9"},
        chat_id=9,
        thread_id=None,
    )
    prompt = fake_plugin.captured_prompt or ""
    # The example lives inside the serialised correction payload JSON.
    payload = json.loads(prompt)
    required_format = payload["system_message"]["required_format"]
    example = required_format["actions"][0]["payload"]["interface_path"]
    assert example == "telegram_bot/9"


@pytest.mark.asyncio
async def test_corrector_example_uses_canonical_interface_id_from_path(monkeypatch):
    """Regression (langfuse 48282d7a-42fe-49a1-9d3a-4db1e1123a21): when
    context["interface"] is a legacy display name ("telegram") that does not
    match the registered interface id, the required_format example must derive
    the action type and interface_path from the authoritative interface_path
    ("telegram_bot/..."). The old code built "message_telegram" +
    "telegram/5208932647" — an unregistered action the model copied verbatim,
    so the correction failed again."""
    fake_plugin = _FakePlugin()

    import core.plugin_instance as plugin_instance

    monkeypatch.setattr(
        plugin_instance, "get_plugin", lambda: fake_plugin, raising=False
    )

    await transport_layer.run_corrector_middleware(
        text="not valid json",
        bot=None,
        context={
            # Legacy display name does NOT match the interface id.
            "interface": "telegram",
            "interface_path": "telegram_bot/5208932647",
        },
        chat_id=5208932647,
        thread_id=None,
    )
    prompt = fake_plugin.captured_prompt or ""
    payload = json.loads(prompt)
    required_format = payload["system_message"]["required_format"]
    example = required_format["actions"][0]
    # The canonical registered type, not the legacy-name-derived "message_telegram".
    assert example["type"] == "send_message"
    assert example["payload"]["interface_path"] == "telegram_bot/5208932647"
    assert 'message_telegram"' not in prompt
