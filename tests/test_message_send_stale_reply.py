"""Stale-identifier fallback ladder for ``send_with_thread_fallback``.

A deleted/purged original message makes ``reply_to_message_id`` permanently
undeliverable ("Message to be replied not found"). The text must still go out:
the ladder strips the stale identifier and retries, never setting a chat-wide
cooldown for data problems.
"""

import time
from types import SimpleNamespace

import pytest

try:
    from telegram.error import BadRequest
    import core.capability_drops as cd
    import interface.message_send_utils as msu
except Exception:
    pytest.skip(
        "python-telegram-bot not installed; skipping telegram send utils tests",
        allow_module_level=True,
    )


@pytest.fixture(autouse=True)
def _clean_state():
    msu._CHAT_COOLDOWNS.clear()
    msu._PENDING_MESSAGES.clear()
    yield
    msu._CHAT_COOLDOWNS.clear()
    msu._PENDING_MESSAGES.clear()


@pytest.mark.asyncio
async def test_stale_reply_target_retries_without_reply_id(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_cortex_send(bot, chat_id, text, **kwargs):
        calls.append(dict(kwargs))
        if "reply_to_message_id" in kwargs:
            raise BadRequest("Message to be replied not found")
        return SimpleNamespace(message_id=42)

    monkeypatch.setattr(msu, "cortex_response_send", fake_cortex_send)
    remembered: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(
        cd, "remember_drops", lambda path, drops: remembered.append((path, drops))
    )

    result = await msu.send_with_thread_fallback(
        object(),
        -1003974332823,
        "hello",
        thread_id=1051,
        reply_to_message_id=987654,
    )

    assert result is not None
    assert len(calls) == 2
    assert calls[0].get("reply_to_message_id") == 987654
    assert calls[1].get("message_thread_id") == 1051
    assert "reply_to_message_id" not in calls[1]
    # A stale reply id is a data problem, not connectivity: no chat cooldown.
    assert -1003974332823 not in msu._CHAT_COOLDOWNS
    # Synth must learn about the degraded delivery on its next turn.
    assert len(remembered) == 1
    path, drops = remembered[0]
    assert path == "telegram_bot/-1003974332823/1051"
    assert drops[0]["feature"] == cd.DROP_REPLY


@pytest.mark.asyncio
async def test_clean_delivery_records_no_drop(monkeypatch) -> None:
    async def fake_cortex_send(bot, chat_id, text, **kwargs):
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(msu, "cortex_response_send", fake_cortex_send)
    remembered: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(
        cd, "remember_drops", lambda path, drops: remembered.append((path, drops))
    )

    await msu.send_with_thread_fallback(
        object(), 12345, "hello", thread_id=9, reply_to_message_id=77
    )

    assert remembered == []


@pytest.mark.asyncio
async def test_drop_recording_failure_never_breaks_delivery(monkeypatch) -> None:
    async def fake_cortex_send(bot, chat_id, text, **kwargs):
        if "reply_to_message_id" in kwargs:
            raise BadRequest("Message to be replied not found")
        return SimpleNamespace(message_id=5)

    monkeypatch.setattr(msu, "cortex_response_send", fake_cortex_send)

    def boom(path, drops):
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(cd, "remember_drops", boom)

    result = await msu.send_with_thread_fallback(
        object(),
        12345,
        "hello",
        reply_to_message_id=77,
        interface_path="telegram_bot/12345",
    )
    assert result is not None


@pytest.mark.asyncio
async def test_stale_reply_then_stale_thread_fully_degrades(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_cortex_send(bot, chat_id, text, **kwargs):
        calls.append(dict(kwargs))
        if "reply_to_message_id" in kwargs:
            raise BadRequest("Message to be replied not found")
        if "message_thread_id" in kwargs:
            raise BadRequest("Message thread not found")
        return SimpleNamespace(message_id=7)

    monkeypatch.setattr(msu, "cortex_response_send", fake_cortex_send)

    result = await msu.send_with_thread_fallback(
        object(),
        12345,
        "hello",
        thread_id=1051,
        reply_to_message_id=987654,
    )

    assert result is not None
    assert len(calls) == 3
    assert calls[-1] == {}
    assert 12345 not in msu._CHAT_COOLDOWNS


@pytest.mark.asyncio
async def test_unknown_error_still_raises_and_cooldowns(monkeypatch) -> None:
    async def fake_cortex_send(bot, chat_id, text, **kwargs):
        raise BadRequest("Message is too long")

    monkeypatch.setattr(msu, "cortex_response_send", fake_cortex_send)

    with pytest.raises(BadRequest):
        await msu.send_with_thread_fallback(object(), 12345, "hello", thread_id=1051)

    # Genuine BadRequests keep the pre-existing chat-wide cooldown behaviour.
    assert 12345 in msu._CHAT_COOLDOWNS
    assert msu._CHAT_COOLDOWNS[12345] > time.time()


def test_is_stale_identifier_error() -> None:
    assert msu._is_stale_identifier_error("Message to be replied not found")
    assert msu._is_stale_identifier_error("Thread not found")
    assert msu._is_stale_identifier_error("Chat not found")
    assert not msu._is_stale_identifier_error("Message is too long")
    assert msu._is_stale_reply_target("Message to be replied not found")


def test_stale_reply_drop_renders_structured_prompt_block() -> None:
    drop = cd.make_drop(
        cd.DROP_REPLY,
        "the replied-to message no longer exists on Telegram; "
        "your reply was delivered without the quote reference",
        "telegram",
    )
    block = cd.render_capability_drops_block([drop])
    assert "CAPABILITY DROPS" in block
    assert "reply_to" in block
    assert "no longer exists" in block
    # No canned user-facing phrasing: Synth acknowledges in its own words.
    assert "in your own words" in block
