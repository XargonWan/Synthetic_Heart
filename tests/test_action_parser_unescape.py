import pytest

from core.action_parser import _maybe_unescape_text_in_payload, _handle_plugin_action


def test_maybe_unescape_text_in_payload_decodes_double_escaped():
    # Use double-escaped sequences so the python literal contains the backslashes
    payload = {
        "text": "Oh, Jay! Che bella domanda \\u2728\\n\\nHai perfettamente ragione"
    }
    # initial contains literal backslashes, not real newline or emoji
    assert "\\n\\n" in payload["text"]
    # Literal \u2728 sequence should be present in the original string
    assert "\\u2728" in payload["text"]

    _maybe_unescape_text_in_payload(payload)

    assert "\n\n" in payload["text"]
    # After unescape, the codepoint should appear as actual emoji
    assert "✨" in payload["text"]


@pytest.mark.asyncio
async def test_handle_plugin_action_forwards_unescaped_text_to_interface(monkeypatch):
    # Create a fake interface and register it
    class FakeInterface:
        def __init__(self):
            self.calls = []

        async def send_message(self, payload, original_message=None):
            self.calls.append(payload)

    fake = FakeInterface()

    # Import registry and set our fake
    import core.core_initializer as core_init

    # Ensure interface registry exists and set our fake under 'test_iface'
    core_init.INTERFACE_REGISTRY["test_iface"] = fake

    action = {
        "type": "message_test_iface",
        "interface": "test_iface",
        "payload": {
            "text": "Hello \\u2728\\n\\nWorld",
            "interface_path": "test_iface/1/0",
        },
    }

    # Call handler
    await _handle_plugin_action(action, context={}, bot=None, original_message=None)

    # Validate that fake interface received unescaped text
    assert len(fake.calls) == 1
    received = fake.calls[0]["text"]
    assert "\n\n" in received
    assert "✨" in received
