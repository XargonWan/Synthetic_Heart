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
async def test_no_fallback_if_partial_success(monkeypatch):
    """If at least one action has already run we should not send a generic
    LLM-failure message when correction retries are exhausted.
    """
    # prepare hooks
    called = {
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
