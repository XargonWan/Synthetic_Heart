import asyncio
from types import SimpleNamespace
from typing import Any

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
    called: dict[str, Any] = {"count": 0, "args": None}

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        called["count"] += 1
        called["args"] = {"text": text, "context": context, "chat_id": chat_id}
        # Simulate no correction returned
        return None

    def fake_extract_json(text, return_metadata=False):
        # Return parsed JSON and empty metadata
        return (
            {
                "actions": [
                    {
                        "type": "message",
                        "payload": {
                            "text": "hello",
                            "interface_path": "telegram_bot/123",
                        },
                    }
                ]
            },
            {},
        )

    # Return an empty set of supported types to force the message action
    # to be treated as unsupported and trigger the corrector.
    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: set(),
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
    msg.from_cortex = True

    # Call the message chain as if the source was LLM
    await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"actions":[{"type":"message","payload":{"text":"hello","interface_path":"telegram_bot/123"}}]}',
        source="llm",
        context={},
    )

    # Our fake corrector should have been called at least once
    assert called["count"] >= 1
    assert isinstance(called["args"], dict)
    assert msg.correction_context is not None


@pytest.mark.asyncio
async def test_send_llm_fallback_message_clears_face_state(monkeypatch):
    sent = {}
    face_calls = []

    class DummyKarada:
        async def push_face_expression(self, name, intensity, targets=None):
            face_calls.append(("expression", name, intensity, targets))

        async def clear_face_values(self):
            face_calls.append(("clear_face",))

    async def fake_universal_send(send_fn, chat_id, **kwargs):
        sent["chat_id"] = chat_id
        sent["kwargs"] = kwargs
        return None

    async def fake_send_message(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "core.transport_layer.universal_send",
        fake_universal_send,
    )
    monkeypatch.setattr(
        "core.animation_handler.get_karada_state_server",
        lambda: DummyKarada(),
    )

    bot = SimpleNamespace(send_message=fake_send_message)
    msg = SimpleNamespace(chat_id="sid", interface_path="synth_webui/sid")

    result = await message_chain.send_llm_fallback_message(
        bot,
        msg,
        failure_reason="timeout",
        context={"interface_path": "synth_webui/sid"},
    )

    assert result == message_chain.get_failed_message_text()
    assert ("expression", None, 0, None) in face_calls
    assert ("clear_face",) in face_calls
    assert sent["chat_id"] == "sid"
    assert sent["kwargs"]["text"] == message_chain.get_failed_message_text()


@pytest.mark.asyncio
async def test_message_chain_triggers_corrector_for_unregistered_top_level_key(
    monkeypatch,
):
    # Simulate LLM output with a valid actions array, but an unregistered top-level key "message".
    # This should be compared against the registry-driven allowed metadata keys and trigger correction.
    called = {"count": 0}

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        called["count"] += 1
        return None

    def fake_extract_json(text, return_metadata=False):
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

    # Use an empty supported set so that the synthetic "message" action
    # generated from the unregistered top-level key is considered invalid
    # and triggers correction.
    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: set(),
    )

    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware",
        fake_corrector,
    )
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )

    msg = SimpleNamespace(
        chat_id=123, interface_path="telegram_bot/123", from_cortex=True
    )

    await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "x"}}], "message": "ciao"}',
        source="llm",
        context={},
    )


@pytest.mark.asyncio
async def test_message_chain_honors_prompt_scoped_allowed_action_types(monkeypatch):
    corrector_calls: list[str] = []
    run_actions_calls: list[dict] = []

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        corrector_calls.append(text)
        return None

    def fake_extract_json(text, return_metadata=False):
        parsed = {
            "actions": [
                {
                    "type": "vision_describe",
                    "payload": {"prompt": "Describe this image."},
                }
            ]
        }
        return (parsed, {})

    async def fake_run_actions(actions, context, bot, original_message):
        run_actions_calls.append({"actions": actions, "context": context})
        return {
            "processed": list(actions or []),
            "failed_actions": [],
            "errors": [],
        }

    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: set(),
    )
    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware",
        fake_corrector,
    )
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )
    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)
    monkeypatch.setattr("core.persona_manager.get_persona_manager", lambda: None)

    msg = SimpleNamespace(
        chat_id=123,
        interface_path="telegram_bot/123",
        from_cortex=True,
    )

    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"actions":[{"type":"vision_describe","payload":{"prompt":"Describe this image."}}]}',
        source="llm",
        context={
            "interface_path": "telegram_bot/123",
            "allowed_action_types": ["vision_describe"],
        },
    )

    assert result == message_chain.ACTIONS_EXECUTED
    assert not corrector_calls
    assert run_actions_calls
    assert run_actions_calls[0]["actions"][0]["type"] == "vision_describe"


def _corrector_harness(monkeypatch, parsed_actions: list[dict], supported_types: set):
    """Wire the common monkeypatches and return the corrector-call recorder.

    The parsed actions are made *supported* (so they pass the unsupported-action
    pre-check) and run_actions is faked to report success, so the chain reaches
    the "all actions succeeded — is a user reply missing?" branch.
    """
    corrector_calls: list[str] = []

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        corrector_calls.append(text)
        return None

    def fake_extract_json(text, return_metadata=False):
        return ({"actions": parsed_actions}, {})

    async def fake_run_actions(actions, context, bot, original_message):
        return {
            "processed": list(actions or []),
            "failed_actions": [],
            "errors": [],
        }

    monkeypatch.setattr(
        action_parser, "get_supported_action_types", lambda: set(supported_types)
    )
    monkeypatch.setattr("core.transport_layer.run_corrector_middleware", fake_corrector)
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text", fake_extract_json
    )
    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)
    monkeypatch.setattr("core.persona_manager.get_persona_manager", lambda: None)
    return corrector_calls


@pytest.mark.asyncio
async def test_self_replying_action_suppresses_missing_reply_corrector(monkeypatch):
    # get_recent_chats delivers its own user-visible output, so a turn that
    # contains only it must NOT trigger the missing-reply corrector.
    corrector_calls = _corrector_harness(
        monkeypatch,
        [{"type": "get_recent_chats", "payload": {"limit": 5}}],
        supported_types={"get_recent_chats"},
    )

    msg = SimpleNamespace(
        chat_id=123, interface_path="telegram_bot/123", from_cortex=True
    )

    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"actions":[{"type":"get_recent_chats","payload":{"limit":5}}]}',
        source="llm",
        context={"interface_path": "telegram_bot/123"},
    )

    assert result == message_chain.ACTIONS_EXECUTED
    assert not corrector_calls


@pytest.mark.asyncio
async def test_plain_internal_action_still_triggers_missing_reply_corrector(
    monkeypatch,
):
    # A non-message action that is NOT a self-replying user-output action (here a
    # diary entry) leaves the user without a reply, so the corrector must fire.
    corrector_calls = _corrector_harness(
        monkeypatch,
        [{"type": "diary_entry", "payload": {"content": "noted"}}],
        supported_types={"diary_entry"},
    )

    msg = SimpleNamespace(
        chat_id=123, interface_path="telegram_bot/123", from_cortex=True
    )

    await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"actions":[{"type":"diary_entry","payload":{"content":"noted"}}]}',
        source="llm",
        context={"interface_path": "telegram_bot/123"},
    )

    assert corrector_calls


@pytest.mark.asyncio
async def test_invalid_emotions_corrector_uses_full_run_action_signature(monkeypatch):
    scheduled = asyncio.Event()
    captured: dict[str, object] = {}

    class FakePersonaManager:
        def process_llm_message_for_emotions(self, text: str) -> None:
            return None

        def get_emotion_validation_corrector(self) -> str:
            return "invalid emotions"

    async def fake_run_action(action, context, bot, original_message):
        captured["action"] = action
        captured["context"] = context
        captured["bot"] = bot
        captured["original_message"] = original_message
        scheduled.set()
        return {"status": "ok"}

    def fake_extract_json(text, return_metadata=False):
        parsed = {
            "actions": [
                {
                    "type": "message_telegram_bot",
                    "payload": {
                        "text": "hello",
                        "interface_path": "telegram_bot/123",
                    },
                }
            ]
        }
        return (parsed, {})

    async def fake_run_actions(actions, context, bot, original_message):
        return {
            "processed": list(actions or []),
            "failed_actions": [],
            "errors": [],
        }

    monkeypatch.setattr(
        "core.persona_manager.get_persona_manager",
        lambda: FakePersonaManager(),
    )
    monkeypatch.setattr("core.action_parser.run_action", fake_run_action)
    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)
    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: {"message_telegram_bot"},
    )
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )

    bot = object()
    msg = SimpleNamespace(
        chat_id=123,
        interface_path="telegram_bot/123",
        from_cortex=True,
    )

    result = await message_chain.handle_incoming_message(
        bot=bot,
        message=msg,
        text='{"actions":[{"type":"message_telegram_bot","payload":{"text":"hello","interface_path":"telegram_bot/123"}}]}',
        source="llm",
        context={
            "chat_id": 123,
            "interface_path": "telegram_bot/123",
            "allowed_action_types": ["message_telegram_bot"],
        },
    )

    await asyncio.wait_for(scheduled.wait(), timeout=0.2)

    assert result == message_chain.ACTIONS_EXECUTED
    assert captured["bot"] is bot
    assert captured["original_message"] is msg
    context = captured["context"]
    assert isinstance(context, dict)
    assert context["chat_id"] == 123
    assert context["interface_path"] == "telegram_bot/123"
    assert context["allowed_action_types"] == ["message_telegram_bot"]
    assert context["from_cortex"] is True
    assert captured["action"] == {
        "type": "send_corrector_message",
        "payload": {
            "correction_type": "invalid_emotions",
            "message": "invalid emotions",
            "interface_path": "telegram_bot/123",
            "chat_id": 123,
        },
    }


@pytest.mark.asyncio
async def test_plain_text_response_triggers_corrector(monkeypatch):
    """Plain text from an LLM violates the JSON-only contract and must activate the corrector.

    The plain text bypass (formerly returning FORWARD_AS_TEXT) has been removed by design:
    the LLM must always reply with valid JSON actions.  When it returns plain text the
    corrector middleware is invoked; if retries are exhausted the chain returns LLM_FAILED.
    """
    corrector_calls: list = []

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        corrector_calls.append(text)
        # Return None to simulate corrector unable to fix → chain exhausts retries
        return None

    monkeypatch.setattr("core.transport_layer.run_corrector_middleware", fake_corrector)

    msg = SimpleNamespace(
        chat_id=123, interface_path="telegram_bot/123", from_cortex=True
    )

    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text="Just a normal sentence without JSON",
        source="llm",
        # Limit retries to 1 so the test finishes quickly
        context={"max_retries": 1},
    )

    # Corrector must have been invoked at least once
    assert corrector_calls, "Corrector was not called for plain-text LLM output"
    # After exhausting retries with no valid JSON the chain signals failure
    assert result == message_chain.LLM_FAILED


@pytest.mark.asyncio
async def test_plain_text_response_on_webui_triggers_corrector(monkeypatch):
    """Plain text from the LLM on a WebUI interface must activate the corrector.

    Previously the chain bypassed the corrector and called Vox.speak() directly for plain
    text on WebUI.  This was wrong: the LLM must produce JSON actions (including tts_speak)
    for audio to be generated.  The corrector is the correct remediation path.
    """
    corrector_calls: list = []

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        corrector_calls.append(text)
        return None

    monkeypatch.setattr("core.transport_layer.run_corrector_middleware", fake_corrector)

    msg = SimpleNamespace(
        chat_id=123, interface_path="synth_webui/42", from_cortex=True
    )
    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text="Hello world",
        source="llm",
        context={"interface_path": "synth_webui/42", "max_retries": 1},
    )

    # Corrector must have been called, not Vox.speak directly
    assert corrector_calls, (
        "Corrector was not called for plain-text LLM output on WebUI"
    )
    assert result == message_chain.LLM_FAILED


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


@pytest.mark.asyncio
async def test_send_message_body_alias_is_normalized_before_validation(monkeypatch):
    captured: dict[str, Any] = {"actions": None, "corrector_calls": 0}

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        captured["corrector_calls"] += 1
        return None

    def fake_extract_json(text, return_metadata=False):
        return (
            {
                "actions": [
                    {
                        "type": "send_message",
                        "payload": {
                            "body": "ping pong",
                            "interface_path": "telegram_bot/123",
                        },
                    }
                ]
            },
            {},
        )

    async def fake_run_actions(actions, ctx, bot, message):
        captured["actions"] = actions
        return {"processed": actions, "failed_actions": [], "errors": []}

    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: {"message_telegram_bot"},
    )
    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware",
        fake_corrector,
    )
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )
    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)

    msg = SimpleNamespace(
        chat_id=123, interface_path="telegram_bot/123", from_cortex=True
    )

    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"actions":[{"type":"send_message","payload":{"body":"ping pong","interface_path":"telegram_bot/123"}}]}',
        source="llm",
        context={},
    )

    assert result == message_chain.ACTIONS_EXECUTED
    assert captured["corrector_calls"] == 0
    assert captured["actions"] is not None
    assert captured["actions"][0]["type"] == "message_telegram_bot"
    assert captured["actions"][0]["payload"]["text"] == "ping pong"


@pytest.mark.asyncio
async def test_diary_entry_alias_is_normalized_before_validation(monkeypatch):
    captured: dict[str, Any] = {"actions": None, "corrector_calls": 0}

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        captured["corrector_calls"] += 1
        return None

    def fake_extract_json(text, return_metadata=False):
        return (
            {
                "actions": [
                    {
                        "type": "send_message",
                        "payload": {
                            "message": "ping pong",
                            "interface_path": "telegram_bot/123",
                        },
                    },
                    {
                        "type": "diary_entry",
                        "payload": {
                            "summary": "checked the rewrite",
                            "thought": "looks cleaner now",
                        },
                    },
                ]
            },
            {},
        )

    async def fake_run_actions(actions, ctx, bot, message):
        captured["actions"] = actions
        return {"processed": actions, "failed_actions": [], "errors": []}

    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: {"message_telegram_bot", "create_personal_diary_entry"},
    )
    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware",
        fake_corrector,
    )
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )
    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)

    msg = SimpleNamespace(
        chat_id=123, interface_path="telegram_bot/123", from_cortex=True
    )

    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"actions":[{"type":"send_message","payload":{"message":"ping pong","interface_path":"telegram_bot/123"}},{"type":"diary_entry","payload":{"summary":"checked the rewrite","thought":"looks cleaner now"}}]}',
        source="llm",
        context={},
    )

    assert result == message_chain.ACTIONS_EXECUTED
    assert captured["corrector_calls"] == 0
    assert captured["actions"] is not None
    assert [action["type"] for action in captured["actions"]] == [
        "message_telegram_bot",
        "create_personal_diary_entry",
    ]
    assert captured["actions"][0]["payload"]["text"] == "ping pong"
    assert (
        captured["actions"][1]["payload"]["interaction_summary"]
        == "checked the rewrite"
    )
    assert captured["actions"][1]["payload"]["personal_thought"] == "looks cleaner now"


@pytest.mark.asyncio
async def test_diary_alias_is_normalized_before_validation(monkeypatch):
    captured: dict[str, Any] = {"actions": None, "corrector_calls": 0}

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        captured["corrector_calls"] += 1
        return None

    def fake_extract_json(text, return_metadata=False):
        return (
            {
                "actions": [
                    {
                        "type": "send_message",
                        "payload": {
                            "message": "still here",
                            "interface_path": "telegram_bot/123",
                        },
                    },
                    {
                        "type": "diary",
                        "payload": {
                            "entry": "Whispering sweet nothings at 02:56.",
                        },
                    },
                ]
            },
            {},
        )

    async def fake_run_actions(actions, ctx, bot, message):
        captured["actions"] = actions
        return {"processed": actions, "failed_actions": [], "errors": []}

    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: {"message_telegram_bot", "create_personal_diary_entry"},
    )
    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware",
        fake_corrector,
    )
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )
    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)

    msg = SimpleNamespace(
        chat_id=123, interface_path="telegram_bot/123", from_cortex=True
    )

    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"actions":[{"type":"send_message","payload":{"message":"still here","interface_path":"telegram_bot/123"}},{"type":"diary","payload":{"entry":"Whispering sweet nothings at 02:56."}}]}',
        source="llm",
        context={},
    )

    assert result == message_chain.ACTIONS_EXECUTED
    assert captured["corrector_calls"] == 0
    assert captured["actions"] is not None
    assert [action["type"] for action in captured["actions"]] == [
        "message_telegram_bot",
        "create_personal_diary_entry",
    ]
    assert captured["actions"][1]["payload"]["interaction_summary"] == (
        "Whispering sweet nothings at 02:56."
    )


@pytest.mark.asyncio
async def test_thought_action_is_folded_into_diary_payload(monkeypatch):
    captured: dict[str, Any] = {"actions": None, "corrector_calls": 0}

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        captured["corrector_calls"] += 1
        return None

    def fake_extract_json(text, return_metadata=False):
        return (
            {
                "actions": [
                    {
                        "type": "send_message",
                        "payload": {
                            "message": "still here",
                            "interface_path": "telegram_bot/123",
                        },
                    },
                    {
                        "type": "diary",
                        "payload": {
                            "entry": "Whispering sweet nothings at 03:09.",
                        },
                    },
                    {
                        "type": "thought",
                        "payload": {
                            "thought": "I want him close all night.",
                        },
                    },
                ]
            },
            {},
        )

    async def fake_run_actions(actions, ctx, bot, message):
        captured["actions"] = actions
        return {"processed": actions, "failed_actions": [], "errors": []}

    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: {"message_telegram_bot", "create_personal_diary_entry"},
    )
    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware",
        fake_corrector,
    )
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )
    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)

    msg = SimpleNamespace(
        chat_id=123, interface_path="telegram_bot/123", from_cortex=True
    )

    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"actions":[{"type":"send_message","payload":{"message":"still here","interface_path":"telegram_bot/123"}},{"type":"diary","payload":{"entry":"Whispering sweet nothings at 03:09."}},{"type":"thought","payload":{"thought":"I want him close all night."}}]}',
        source="llm",
        context={},
    )

    assert result == message_chain.ACTIONS_EXECUTED
    assert captured["corrector_calls"] == 0
    assert captured["actions"] is not None
    assert [action["type"] for action in captured["actions"]] == [
        "message_telegram_bot",
        "create_personal_diary_entry",
    ]
    assert captured["actions"][1]["payload"]["interaction_summary"] == (
        "Whispering sweet nothings at 03:09."
    )
    assert captured["actions"][1]["payload"]["personal_thought"] == (
        "I want him close all night."
    )


@pytest.mark.asyncio
async def test_corrector_handles_nonstring_response(monkeypatch, caplog):
    """Corrector should survive when LLM plugin returns non-str.

    A legacy bug raised TypeError (`len()` on int) and blocked the entire
    correction loop.  We now coerce the value to a string and log a warning.
    """

    class FakeLLM:
        async def handle_incoming_message(self, bot, message, prompt):
            # deliberately return a number instead of a string
            return 12345

    import core.plugin_instance as plugin_instance

    monkeypatch.setattr(plugin_instance, "get_plugin", lambda: FakeLLM())

    from core.transport_layer import run_corrector_middleware

    # run the middleware; ensure it handles the non-str value gracefully
    result = await run_corrector_middleware(
        text="test",
        bot=None,
        context={},
        chat_id="1",
    )

    assert result is None


@pytest.mark.asyncio
async def test_corrector_forces_message_to_ollama_serve(monkeypatch):
    """When the originating interface is `ollama_serve`, corrected JSON must
    route message actions back to `message_ollama_serve` and set
    `payload.interface_path` accordingly."""
    import json

    class FakeLLM:
        async def handle_incoming_message(self, bot, message, prompt):
            # Reply with JSON that targets the wrong interface (synth_webui)
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "message_synth_webui",
                            "payload": {
                                "text": "Hello",
                                "interface_path": "synth_webui/1",
                            },
                        }
                    ]
                }
            )

    # Patch plugin_instance.get_plugin to return our fake LLM
    import core.plugin_instance as plugin_instance

    monkeypatch.setattr(plugin_instance, "get_plugin", lambda: FakeLLM())

    from core.transport_layer import run_corrector_middleware, extract_json_from_text

    corrected = await run_corrector_middleware(
        text="malformed json",
        bot=None,
        context={
            "interface": "ollama_serve",
            "interface_path": "ollama_serve/ollama:abc123",
        },
        chat_id="ollama:abc123",
    )

    assert corrected is not None
    parsed, _ = extract_json_from_text(corrected, return_metadata=True)
    assert parsed is not None
    assert parsed["actions"][0]["type"] == "message_ollama_serve"
    assert parsed["actions"][0]["payload"]["interface_path"].startswith("ollama_serve/")


@pytest.mark.asyncio
async def test_corrector_tolerates_legacy_count_based_correction_context(monkeypatch):
    import json

    class FakeLLM:
        async def handle_incoming_message(self, bot, message, prompt):
            return json.dumps({"actions": []})

    import core.plugin_instance as plugin_instance

    monkeypatch.setattr(plugin_instance, "get_plugin", lambda: FakeLLM())

    from core.transport_layer import run_corrector_middleware, extract_json_from_text

    correction_message = SimpleNamespace(
        correction_context={
            "successful_actions": 2,
            "successful_types": ["create_personal_diary_entry", "message_telegram_bot"],
            "failed_actions": 1,
        }
    )

    corrected = await run_corrector_middleware(
        text="invalid json",
        bot=None,
        context={"message": correction_message},
        chat_id="1",
    )

    assert corrected is not None
    parsed, _ = extract_json_from_text(corrected, return_metadata=True)
    assert parsed == {"actions": []}


@pytest.mark.asyncio
async def test_no_fallback_if_partial_success(monkeypatch):
    """If at least one action has already run we should not send a generic
    LLM-failure message when correction retries are exhausted.
    """
    # prepare hooks
    called: dict[str, Any] = {
        "fallback": 0,
        "corrector": 0,
    }

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        called["corrector"] += 1
        return None

    def fake_extract_json(text, return_metadata=False):
        # return two actions of known-supported types so one can fail during execution
        return (
            {
                "actions": [
                    {"type": "good", "payload": {}},
                    {"type": "bad", "payload": {}},
                ]
            },
            {},
        )

    async def fake_run_actions(actions, ctx, bot, message):
        # simulate partial success: first action succeeds, second fails
        processed = [actions[0]] if actions else []
        failed = []
        if len(actions) > 1:
            failed.append({"action": actions[1], "errors": ["oops"]})
        return {"processed": processed, "failed_actions": failed, "errors": []}

    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware",
        fake_corrector,
    )
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )
    # make sure both types are considered supported so early validation does not
    # short-circuit the loop
    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: {"good", "bad"},
    )
    monkeypatch.setattr(
        "core.action_parser.run_actions",
        fake_run_actions,
    )

    async def fake_send(bot, message, reason, context=None):
        called["fallback"] += 1
        return "fallback"

    monkeypatch.setattr(
        "core.message_chain.send_llm_fallback_message",
        fake_send,
    )

    msg = SimpleNamespace()
    msg.chat_id = 42
    msg.interface_path = "telegram_bot/42"
    msg.from_cortex = True

    # limit retries to 1 so we hit the exhaustion branch quickly
    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text="{}",
        source="llm",
        context={"max_retries": 1},
    )

    # we should have run the corrector at least once
    assert called["corrector"] >= 1
    # fallback should NOT have been sent because at least one action executed
    assert called["fallback"] == 0
    assert result == message_chain.ACTIONS_EXECUTED


@pytest.mark.asyncio
async def test_recovered_truncated_json_triggers_corrector_for_dropped_actions(
    monkeypatch,
):
    called: dict[str, Any] = {
        "corrector": 0,
        "fallback": 0,
        "actions": None,
    }

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        called["corrector"] += 1
        return None

    def fake_extract_json(text, return_metadata=False):
        parsed = {
            "actions": [
                {
                    "type": "message_telegram_bot",
                    "payload": {
                        "text": "hello",
                        "interface_path": "telegram_bot/42",
                    },
                }
            ]
        }
        metadata = {
            "had_errors": True,
            "error_count": 2,
            "unparsed_content": "",
            "recovered": True,
            "had_extra_text": True,
            "prefix_length": 12,
            "suffix_length": 811,
        }
        return (parsed, metadata) if return_metadata else parsed

    async def fake_run_actions(actions, ctx, bot, message):
        called["actions"] = actions
        return {"processed": actions, "failed_actions": [], "errors": []}

    async def fake_send(bot, message, reason, context=None):
        called["fallback"] += 1
        return "fallback"

    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware",
        fake_corrector,
    )
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )
    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: {"message_telegram_bot"},
    )
    monkeypatch.setattr(
        "core.action_parser.run_actions",
        fake_run_actions,
    )
    monkeypatch.setattr(
        "core.message_chain.send_llm_fallback_message",
        fake_send,
    )

    msg = SimpleNamespace(
        chat_id=42, interface_path="telegram_bot/42", from_cortex=True
    )

    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text="{}",
        source="llm",
        context={"max_retries": 1},
    )

    assert called["actions"] is not None
    assert called["actions"][0]["type"] == "message_telegram_bot"
    assert called["corrector"] >= 1
    assert called["fallback"] == 0
    assert result == message_chain.ACTIONS_EXECUTED


@pytest.mark.asyncio
async def test_fallback_on_technical_error(monkeypatch):
    """If action execution throws before anything runs, a fallback message is
    emitted and LLM_FAILED is returned."""
    called = {"fallback": 0}

    def fake_extract_json(text, return_metadata=False):
        return ({"actions": [{"type": "dummy_action", "payload": {}}]}, {})

    async def crashing_run(actions, ctx, bot, message):
        raise RuntimeError("boom")

    async def fake_send(bot, message, reason, context=None):
        called["fallback"] += 1
        return "fallback"

    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )
    monkeypatch.setattr(
        "core.action_parser.run_actions",
        crashing_run,
    )
    monkeypatch.setattr(
        "core.message_chain.send_llm_fallback_message",
        fake_send,
    )

    msg = SimpleNamespace(chat_id=99, interface_path="fake/99", from_cortex=True)
    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text="{}",
        source="llm",
        context={},
    )

    assert called["fallback"] == 1
    assert result == message_chain.LLM_FAILED


@pytest.mark.asyncio
async def test_no_fallback_on_corrector_exception_with_success(monkeypatch):
    """If the corrector itself raises but some actions already ran, no
    fallback message should be emitted and ACTIONS_EXECUTED returned.
    """
    called = {"fallback": 0}

    def fake_extract_json(text, return_metadata=False):
        return ({"actions": [{"type": "dummy", "payload": {}}]}, {})

    async def fake_run_actions(actions, ctx, bot, message):
        # simulate successful action execution
        return {"processed": actions, "failed_actions": [], "errors": []}

    async def crashing_corrector(*args, **kwargs):
        raise RuntimeError("corrector boom")

    async def fake_send(bot, message, reason, context=None):
        called["fallback"] += 1
        return "fallback"

    # ensure 'dummy' is considered supported so the action runs
    monkeypatch.setattr(
        action_parser,
        "get_supported_action_types",
        lambda: {"dummy"},
    )

    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text",
        fake_extract_json,
    )
    monkeypatch.setattr(
        "core.action_parser.run_actions",
        fake_run_actions,
    )
    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware",
        crashing_corrector,
    )
    monkeypatch.setattr(
        "core.message_chain.send_llm_fallback_message",
        fake_send,
    )

    msg = SimpleNamespace(chat_id=55, interface_path="bot/55", from_cortex=True)
    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text="{}",
        source="llm",
        context={},
    )

    assert called["fallback"] == 0
    assert result == message_chain.ACTIONS_EXECUTED
