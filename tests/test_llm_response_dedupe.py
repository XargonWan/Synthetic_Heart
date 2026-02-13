import pytest

from interface.message_send_utils import llm_response_send


@pytest.mark.asyncio
async def test_llm_response_send_dedupe(monkeypatch):
    sent = []

    async def fake_send(bot, chat_id, text, *a, **kw):
        sent.append((chat_id, text))
        return "ok"

    monkeypatch.setattr("interface.message_send_utils._send_with_retry", fake_send)

    # First send should go through
    res1 = await llm_response_send("bot", 123, "Hello there")
    assert res1 == "ok"

    # Second identical send within dedupe window should be suppressed
    res2 = await llm_response_send("bot", 123, "Hello there")
    assert res2 is None

    assert len(sent) == 1
