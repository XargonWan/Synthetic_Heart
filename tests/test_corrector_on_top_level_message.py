import pytest
from types import SimpleNamespace

from core.action_parser import corrector_orchestrator


@pytest.mark.asyncio
async def test_corrector_invoked_when_top_level_message_without_message_action(
    monkeypatch,
):
    called = {}

    async def fake_run_corrector_middleware(
        text, bot=None, context=None, chat_id=None, interface_path=None, **kwargs
    ):
        called["text"] = text
        called["bot"] = bot
        called["context"] = context
        called["chat_id"] = chat_id
        called["interface_path"] = interface_path
        return None

    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware", fake_run_corrector_middleware
    )
    monkeypatch.setattr(
        "core.action_parser.run_corrector_middleware", fake_run_corrector_middleware
    )

    # Build a JSON-like LLM response: has actions that are internal only and a top-level message
    llm_text = """{
        "actions": [
            {"type": "use_animation", "payload": {"animation_state": "think"}},
            {"type": "create_personal_diary_entry", "payload": {"interaction_summary": "x"}}
        ],
        "message": "This is a reply that should be sent to the user"
    }"""

    message = SimpleNamespace()
    message.from_cortex = True
    message.chat_id = 12345
    message.interface_path = "telegram_bot/12345"

    # include a cortex-origin flag in context to mirror real transport use
    result = await corrector_orchestrator(
        llm_text,
        context={"interface": "telegram", "from_cortex": True},
        bot=None,
        message=message,
        max_retries=1,
    )

    # Corrector should have been invoked; result may be True because valid actions were executed
    # and correction is requested for invalid synthetic actions
    assert result in (True, False)
    assert "context" in called
    ctx = called["context"]
    # Correction context should include the original message under 'message'
    assert "message" in ctx
    assert ctx["message"] is message
    # The correction instruction should mention that a 'message' was provided and failed
    assert "message" in called["text"] or (
        ctx.get("correction_context")
        and "message" in ctx["correction_context"].get("instruction", "")
    )
    # Make sure our origin flag is passed via context when message isn't useful
    assert ctx.get("from_cortex")

    # If we drop all actions (simulate only invalid ones), the orchestrator
    # should return False (blocking) even without middleware involvement.
    called.clear()
    bad = '{"type":"message_unknown","payload":{}}'
    result2 = await corrector_orchestrator(
        bad,
        context={"interface": "telegram", "from_cortex": True},
        bot=None,
        message=message,
        max_retries=1,
    )
    assert result2 is False


@pytest.mark.asyncio
async def test_corrector_orchestrator_auto_fix_numeric_string(monkeypatch):
    """If run_actions fails because a numeric field was a string, the orchestrator
    should normalize/auto‑fix and retry instead of blocking.
    """
    calls = []

    async def fake_run_actions(actions, ctx, bot, msg):
        # record each invocation and simulate a type error on first try
        calls.append([dict(a) for a in actions])
        if len(calls) == 1:
            return {
                "processed": [],
                "failed_actions": [
                    {
                        "action": actions[0],
                        "errors": ["payload.reply_to_message_id must be an int"],
                    }
                ],
                "errors": ["payload.reply_to_message_id must be an int"],
            }
        else:
            return {"processed": actions, "failed_actions": [], "errors": []}

    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)

    message = SimpleNamespace()
    message.from_cortex = True

    text = '{"actions":[{"type":"message_telegram_bot","payload":{"text":"hi","interface_path":"telegram_bot/1","reply_to_message_id":" 42 "}}]}'
    res = await corrector_orchestrator(
        text, {"from_cortex": True}, bot=None, message=message
    )
    assert res is True
    # two calls: initial attempt plus auto-fixed retry
    assert len(calls) == 2
    # second call should have coerced the id to int
    assert isinstance(calls[1][0]["payload"]["reply_to_message_id"], int)


@pytest.mark.asyncio
async def test_message_chain_filters_duplicate_actions_on_retry(monkeypatch):
    """When a correction retry occurs, previously-successful action types
    should be removed from the next run_actions payload.

    We stub out run_actions to simulate a partial success on the first
    invocation (first action processed, second action failed) and return the
    same text from the corrector so the chain would normally re-send both
    actions.  The filter logic added to message_chain should strip the
    successful type before the second call.
    """
    from core import message_chain

    call_types = []

    async def fake_run_actions(actions, ctx, bot, message):
        types = [
            a.get("type") or a.get("action")
            for a in (actions or [])
            if isinstance(a, dict)
        ]
        call_types.append(types)
        processed = []
        failed = []
        if actions:
            processed.append(actions[0])
            if len(actions) > 1:
                failed.append({"action": actions[1], "errors": ["fail"]})
        return {"processed": processed, "failed_actions": failed, "errors": []}

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None, **kwargs
    ):
        # always return same text so the loop re-parses the original actions
        return text

    def fake_extract_json(text, return_metadata=False):
        return (
            {
                "actions": [
                    {"type": "first", "payload": {}},
                    {"type": "second", "payload": {}},
                ]
            },
            {},
        )

    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text", fake_extract_json
    )
    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)
    monkeypatch.setattr("core.transport_layer.run_corrector_middleware", fake_corrector)
    monkeypatch.setattr(
        "core.action_parser.get_supported_action_types",
        lambda: {"first", "second"},
    )

    msg = SimpleNamespace(chat_id=1, interface_path="telegram_bot/1", from_cortex=True)
    # allow two retries so the second invocation happens
    await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text="{}",
        source="llm",
        context={"max_retries": 2},
    )

    # ensure we called run_actions twice and the second call dropped the
    # previously successful "first" action
    assert len(call_types) == 2
    assert call_types[0] == ["first", "second"]
    assert call_types[1] == ["second"]


@pytest.mark.asyncio
async def test_partial_success_filters_non_successful_action(monkeypatch):
    """Similar to the previous test but emphasises the corrector loop.

    We simulate one action succeeding and one failing on the first
    run_actions call. The fake corrector simply echoes back the same text so
    that parsing will produce both actions again.  The second run_actions
    invocation should therefore only receive the action that failed earlier
    (i.e. non-successful).
    """
    from core import message_chain

    call_types = []

    async def fake_run_actions(actions, ctx, bot, message):
        types = [
            a.get("type") or a.get("action")
            for a in (actions or [])
            if isinstance(a, dict)
        ]
        call_types.append(types)
        processed = []
        failed = []
        if actions:
            # mark first item as processed (success)
            processed.append(actions[0])
            # second action always fails
            if len(actions) > 1:
                failed.append({"action": actions[1], "errors": ["error"]})
        return {"processed": processed, "failed_actions": failed, "errors": []}

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None, **kwargs
    ):
        return text

    def fake_extract_json(text, return_metadata=False):
        return (
            {
                "actions": [
                    {"type": "alpha", "payload": {}},
                    {"type": "beta", "payload": {}},
                ]
            },
            {},
        )

    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text", fake_extract_json
    )
    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)
    monkeypatch.setattr("core.transport_layer.run_corrector_middleware", fake_corrector)
    monkeypatch.setattr(
        "core.action_parser.get_supported_action_types",
        lambda: {"alpha", "beta"},
    )

    msg = SimpleNamespace(chat_id=2, interface_path="telegram_bot/2", from_cortex=True)
    await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text="{}",
        source="llm",
        context={"max_retries": 2},
    )

    assert len(call_types) == 2
    assert call_types[0] == ["alpha", "beta"]
    assert call_types[1] == ["beta"]
