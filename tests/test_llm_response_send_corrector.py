import pytest

from interface.message_send_utils import cortex_response_send


def _patch_delivery_recorder(monkeypatch):
    """Capture delivery-failure records instead of writing them to the live DB.

    The corrector branches now record a ``delivery_failed`` row; without this the
    tests would append real rows to the deployment's failure log.
    """
    recorded = []

    async def fake_record(
        reason, *, chat_id=None, interface_path=None, kwargs=None, text=None
    ):
        recorded.append(
            {
                "reason": reason,
                "chat_id": chat_id,
                "interface_path": interface_path,
                "text": text,
            }
        )

    monkeypatch.setattr(
        "interface.message_send_utils._record_delivery_failure",
        fake_record,
        raising=False,
    )
    return recorded


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
    recorded = _patch_delivery_recorder(monkeypatch)

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
    # The context must carry the REGISTERED interface id plus a structural path.
    # The corrector's example builder maps the interface through a table keyed by
    # registered ids, so the legacy display name this sender used to pass
    # ("telegram") made it teach the unregistered ``message_telegram`` — which
    # the model copied back, losing the reply entirely (live 2026-09-21, langfuse
    # feca9072-0abe-46ef-90da-f1409723088e).
    assert called["context"]["interface"] == "telegram_bot"
    assert called["context"]["interface_path"] == "telegram_bot/321"
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

    # Every undelivered reply leaves a ROW. A user-facing message that vanishes
    # with no record anywhere is undiagnosable - the two 2026-09-21 losses had to
    # be reconstructed from Langfuse traces plus raw logs precisely because no
    # failure row existed.
    assert [r["reason"] for r in recorded] == [
        "corrector_orchestrator blocked the message",
        "corrector_orchestrator declined; blocked instead of sending unparseable JSON",
    ]
    assert all(r["chat_id"] == 321 for r in recorded)
    assert all(r["interface_path"] == "telegram_bot/321" for r in recorded)


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
    recorded = _patch_delivery_recorder(monkeypatch)

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
    # nothing should have been sent, and the loss is recorded
    assert sent == []
    assert [r["reason"] for r in recorded] == [
        "corrector_orchestrator blocked the message"
    ]


@pytest.mark.asyncio
async def test_dedupe_not_required_when_blocking(monkeypatch):
    """If corrector blocks, multiple identical calls shouldn't trigger sends."""

    async def block_corrector(text, context=None, bot=None, message=None):
        return False

    monkeypatch.setattr("core.action_parser.corrector_orchestrator", block_corrector)
    recorded = _patch_delivery_recorder(monkeypatch)

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
    # ...and each blocked attempt left its own delivery row
    assert len(recorded) == 2


@pytest.mark.asyncio
async def test_roleplay_text_opening_with_a_brace_is_sent_not_corrected(monkeypatch):
    """A reply whose prose OPENS with ``{`` is a message, not broken JSON.

    This persona writes physical actions in braces, so live reply texts look like
    ``{the crack of your palm against my ass lands sharp and I jolt forward...}``.
    The sender classified any text starting with ``{`` as malformed JSON and
    handed the entire reply to the corrector, which answered with an ACTION
    instead of the text: no send was ever attempted and the reply never reached
    the chat (live 2026-09-21, langfuse feca9072-0abe-46ef-90da-f1409723088e and
    b822895b-b87d-43f8-b535-2bbe9c3d31c7; the last delivery to that chat was
    11:24:34 while two later turns produced no send line at all).
    """
    called = {}

    async def fake_corrector(text, context=None, bot=None, message=None):
        called["corrector"] = True
        return False

    monkeypatch.setattr("core.action_parser.corrector_orchestrator", fake_corrector)
    recorded = _patch_delivery_recorder(monkeypatch)

    sent = []

    async def fake_send(bot, chat_id, text, *a, **kw):
        sent.append((chat_id, text))
        return "ok"

    monkeypatch.setattr("interface.message_send_utils._send_with_retry", fake_send)

    roleplay = (
        "{the crack of your palm against my ass lands sharp and I jolt forward "
        "with a yelp that turns straight into a moan, nails digging into your "
        "thighs for balance} Mnnh-fuck, there it is. Again. {I bounce, quick and "
        "filthy, thighs burning}"
    )
    res = await cortex_response_send("bot", 321, roleplay)

    assert res == "ok"
    assert sent == [(321, roleplay)]
    assert "corrector" not in called
    assert recorded == []


@pytest.mark.asyncio
async def test_broken_action_envelope_still_routes_to_the_corrector(monkeypatch):
    """The envelope guard must NOT disarm the corrector for real action payloads.

    Guard against over-shooting: text that starts with ``{`` AND carries
    action-schema keys is still a broken envelope and must be corrected.
    """
    called = {}

    async def fake_corrector(text, context=None, bot=None, message=None):
        called["corrector"] = True
        return False

    monkeypatch.setattr("core.action_parser.corrector_orchestrator", fake_corrector)
    recorded = _patch_delivery_recorder(monkeypatch)

    sent = []

    async def fake_send(bot, chat_id, text, *a, **kw):
        sent.append((chat_id, text))
        return "ok"

    monkeypatch.setattr("interface.message_send_utils._send_with_retry", fake_send)

    # Unparseable (trailing comma) but unmistakably an action envelope.
    broken = '{"actions": [{"type": "send_message", "payload": {"text": "hi",}}]}'
    res = await cortex_response_send("bot", 321, broken)

    assert res is None
    assert sent == []
    assert called.get("corrector") is True
    assert (
        recorded
        and recorded[0]["reason"] == "corrector_orchestrator blocked the message"
    )
