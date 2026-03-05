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
    result = await message_chain.handle_incoming_message(
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

    result = await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "x"}}], "message": "ciao"}',
        source="llm",
        context={},
    )


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
