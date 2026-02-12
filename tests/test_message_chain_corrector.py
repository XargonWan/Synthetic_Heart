import asyncio
from types import SimpleNamespace

import pytest

from core import message_chain
from core import action_parser


@pytest.mark.asyncio
async def test_validate_action_rejects_unknown():
    # Validate that unknown action types are rejected by the validator
    valid, errors = action_parser.validate_action({"type": "message", "payload": {}})
    assert not valid
    assert any("Unsupported type 'message'" in err for err in errors)


@pytest.mark.asyncio
async def test_message_chain_triggers_corrector_for_unregistered_action(monkeypatch):
    # Simulate LLM output containing an unregistered action type 'message'
    called = {"count": 0, "args": None}

    async def fake_corrector(text, bot=None, context=None, chat_id=None, thread_id=None):
        called["count"] += 1
        called["args"] = {"text": text, "context": context, "chat_id": chat_id}
        # Simulate no correction returned
        return None

    async def fake_extract_json(text, return_metadata=False):
        # Return parsed JSON and empty metadata
        return ({"actions": [{"type": "message", "payload": {"text": "hello", "interface_path": "telegram_bot/123"}}]}, {})

    # Ensure supported types do NOT include bare 'message'
    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: set(["message_telegram_bot", "message_discord_bot"]),
    )

    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware",
        fake_corrector,
    )
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )

    msg = SimpleNamespace()
    msg.chat_id = 123
    msg.interface_path = "telegram_bot/123"
    msg.from_llm = True

    # Call the message chain as if the source was LLM
    result = await message_chain.handle_incoming_message(
        bot=None, message=msg, text='{"actions":[{"type":"message","payload":{"text":"hello","interface_path":"telegram_bot/123"}}]}', source='llm', context={}
    )

    # Our fake corrector should have been called at least once
    assert called["count"] >= 1
    assert isinstance(called["args"], dict)
    assert msg.correction_context is not None


@pytest.mark.asyncio
async def test_message_chain_triggers_corrector_for_unregistered_top_level_key(monkeypatch):
    # Simulate LLM output with a valid actions array, but an unregistered top-level key "message".
    # This should be compared against the registry-driven allowed metadata keys and trigger correction.
    called = {"count": 0}

    async def fake_corrector(text, bot=None, context=None, chat_id=None, thread_id=None):
        called["count"] += 1
        return None

    async def fake_extract_json(text, return_metadata=False):
        parsed = {
            "actions": [
                {
                    "type": "create_personal_diary_entry",
                    "payload": {"interaction_summary": "x"},
                }
            ],
            "message": "ciao",
            # feelings is allowed metadata (registered by persona_manager)
            "feelings": {"happy": 5.0},
        }
        return (parsed, {})

    # Supported actions do not include bare 'message'
    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: set(["create_personal_diary_entry", "message_telegram_bot", "message_discord_bot", "use_animation"]),
    )

    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware",
        fake_corrector,
    )
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )

    msg = SimpleNamespace(chat_id=123, interface_path="telegram_bot/123", from_llm=True)

    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "x"}}], "message": "ciao"}',
        source="llm",
        context={},
    )

    assert called["count"] >= 1
    assert msg.correction_context is not None


@pytest.mark.asyncio
async def test_normalize_message_unknown_obeys_supported_actions(monkeypatch):
    # If the interface-specific action is supported, normalization should occur
    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: set(["message_telegram_bot"]),
    )

    actions = [{"type": "message_unknown", "payload": {"text": "hi"}}]
    normalized = message_chain._normalize_message_unknown(actions, "telegram_bot/123")
    assert normalized[0]["type"] == "message_telegram_bot"

    # If target action is NOT supported, normalization should be skipped
    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: set([]),
    )
    actions2 = [{"type": "message_unknown", "payload": {"text": "hi"}}]
    normalized2 = message_chain._normalize_message_unknown(actions2, "telegram_bot/123")
    assert normalized2[0]["type"] == "message_unknown"
