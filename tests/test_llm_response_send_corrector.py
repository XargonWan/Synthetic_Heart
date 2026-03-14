import pytest

from interface.message_send_utils import cortex_response_send


@pytest.mark.asyncio
async def test_corrector_flags_and_block(monkeypatch):
    """Ensure cortex_response_send marks the dummy message correctly and blocks when
    corrector says False or returns None for JSON-like text."""

    called = {}

    async def fake_corrector(text, context=None, bot=None, message=None):
        # record what we got
        called["text"] = text
        called["context"] = context.copy() if context else {}
        called["message"] = message
        # simulate an invalid payload: block
        return False

    monkeypatch.setattr("core.action_parser.corrector_orchestrator", fake_corrector)

    sent = []

    async def fake_send(bot, chat_id, text, *a, **kw):
        sent.append((chat_id, text))
        return "ok"

    monkeypatch.setattr("interface.message_send_utils._send_with_retry", fake_send)

    # send a JSON-like string that cannot be parsed (extra comma), triggering the corrector
    text = '{"type":"message_telegram_bot","payload":{"text":"hi",}}'  # invalid trailing comma but contains both braces
    res = await cortex_response_send("bot", 321, text)
    assert res is None
    # the fake_corrector should have seen the cortex flag
    assert "message" in called
    assert getattr(called["message"], "from_cortex", False)
    assert called["context"].get("from_cortex") is True
    # no send attempt should have happened
    assert sent == []

    # if corrector returns None but text is JSON-like, still block
    async def none_corrector(text, context=None, bot=None, message=None):
        called["marker"] = "none"
        return None

    monkeypatch.setattr("core.action_parser.corrector_orchestrator", none_corrector)

    sent.clear()
    res2 = await cortex_response_send("bot", 321, text)
    assert res2 is None
    assert sent == []

    # when text is plain non-JSON, corrector should not be invoked and send should happen
    called.clear()
    text2 = "Hello world"
    res3 = await cortex_response_send("bot", 321, text2)
    assert res3 == "ok"
    assert "text" not in called


@pytest.mark.asyncio
async def test_corrector_invoked_for_extra_top_level_keys(monkeypatch):
    """If the LLM JSON includes unregistered top-level keys the corrector should
    be executed before any actions are run.
    """
    called = {}

    async def fake_corrector(text, context=None, bot=None, message=None):
        called["text"] = text
        called["context"] = context.copy() if context else {}
        called["message"] = message
        # tell the caller that correction blocked the message
        return False

    monkeypatch.setattr("core.action_parser.corrector_orchestrator", fake_corrector)

    sent = []

    async def fake_send(bot, chat_id, text, *a, **kw):
        sent.append((chat_id, text))
        return "ok"

    monkeypatch.setattr("interface.message_send_utils._send_with_retry", fake_send)

    # valid JSON with actions plus an extra 'message' key
    text = '{"actions":[{"type":"message_telegram_bot","payload":{"text":"hi","interface_path":"t/1"}}],"message":"oops"}'
    res = await cortex_response_send("bot", 321, text)
    assert res is None
    assert called["text"] == text
    # ensure corrector saw cortical flag in context
    assert called["context"].get("from_cortex") is True
    # nothing should have been sent
    assert sent == []


@pytest.mark.asyncio
async def test_dedupe_not_required_when_blocking(monkeypatch):
    """If corrector blocks, multiple identical calls shouldn't trigger sends."""

    async def block_corrector(text, context=None, bot=None, message=None):
        return False

    monkeypatch.setattr("core.action_parser.corrector_orchestrator", block_corrector)

    sent = []

    async def fake_send(bot, chat_id, text, *a, **kw):
        sent.append((chat_id, text))
        return "ok"

    monkeypatch.setattr("interface.message_send_utils._send_with_retry", fake_send)

    text = '{"type":"message_telegram_bot","payload":{"text":"dup"}}'
    await cortex_response_send("bot", 1, text)
    await cortex_response_send("bot", 1, text)
    # corrector blocked both times, so no sends at all
    assert sent == []
