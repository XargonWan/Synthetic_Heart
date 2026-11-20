"""What the model is shown for the message a user replies to."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core import media_descriptions, quoted_message
from core.agent_core import AgentLoopManager
from core.message_queue import _reply_context_from_message
from core.quoted_message import QUOTE_MAX_CHARS, clip, media_kind, quote_text


def _reply(**attrs: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"text": None, "caption": None, "message_id": 273}
    base.update(attrs)
    return SimpleNamespace(**base)


def _stored(monkeypatch: pytest.MonkeyPatch, value: str | None) -> list[tuple]:
    calls: list[tuple] = []

    async def _lookup(interface_path: Any, message_id: Any) -> str | None:
        calls.append((interface_path, message_id))
        return value

    monkeypatch.setattr(media_descriptions, "lookup", _lookup)
    return calls


def test_clip_cuts_on_a_word_and_marks_the_cut() -> None:
    long = ("parola " * 500).strip()
    out = clip(long, 100)
    assert len(out) <= 101 and out.endswith("…") and not out[:-1].endswith("paro")
    assert clip("corto", 100) == "corto"


def test_media_kind_reads_attributes_of_every_interface() -> None:
    assert media_kind(_reply(photo=[object()])) == "photo"
    assert media_kind(_reply(voice=object())) == "voice message"
    assert media_kind(_reply(attachments=[{"id": 1}])) == "attachment"  # Discord/Fluxer
    assert media_kind(_reply()) is None


@pytest.mark.asyncio
async def test_a_text_quote_is_the_text_and_is_not_cut_at_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stored(monkeypatch, None)
    text = "x" * 1500
    assert await quote_text(_reply(text=text), "telegram/1") == text, (
        "a 430-character message was cut at 400"
    )
    assert len(await quote_text(_reply(text="y" * 5000), "telegram/1")) <= (
        QUOTE_MAX_CHARS + 1
    )


@pytest.mark.asyncio
async def test_a_caption_counts_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _stored(monkeypatch, None)
    out = await quote_text(_reply(caption="guarda la scritta", photo=[1]), "telegram/1")
    assert out == "guarda la scritta"


@pytest.mark.asyncio
async def test_a_captionless_photo_is_quoted_as_what_iris_saw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stored(monkeypatch, "an anime girl with a glowing sword")
    out = await quote_text(_reply(photo=[1]), "telegram/31321637")
    assert "an anime girl with a glowing sword" in out and "photo" in out
    assert calls == [("telegram/31321637", 273)]


@pytest.mark.asyncio
async def test_a_photo_nobody_described_still_says_it_is_a_photo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stored(monkeypatch, None)
    out = await quote_text(_reply(photo=[1]), "telegram/1")
    assert "photo" in out and "not available" in out


@pytest.mark.asyncio
async def test_nothing_to_quote_gives_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stored(monkeypatch, None)
    assert await quote_text(_reply(), "telegram/1") == ""


@pytest.mark.asyncio
async def test_the_agent_lane_context_carries_the_media_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stored(monkeypatch, "a cover that says SPADA")
    message = SimpleNamespace(
        interface_path="telegram/31321637",
        reply_to_message=_reply(
            photo=[1], from_user=SimpleNamespace(full_name="Jay Cheshire")
        ),
    )
    ctx = await _reply_context_from_message(message)
    assert ctx is not None
    assert ctx["sender_name"] == "Jay Cheshire"
    assert "a cover that says SPADA" in ctx["text"]
    assert await _reply_context_from_message(SimpleNamespace()) is None


def test_the_agent_prompt_shows_the_quoted_message() -> None:
    prompt = AgentLoopManager._build_agent_prompt(
        "answer the user",
        [],
        None,
        {"reply_context": {"sender_name": "Rekku", "text": "the earlier message"}},
    )
    text = prompt["input"]["payload"]["text"]
    assert "QUOTED MESSAGE" in text and "the earlier message" in text
    assert "Rekku" in text

    plain = AgentLoopManager._build_agent_prompt("answer the user", [], None, {})
    assert "QUOTED MESSAGE" not in plain["input"]["payload"]["text"]


def test_the_chat_key_ignores_a_thread_suffix() -> None:
    assert media_descriptions.chat_key("telegram/123/456") == "telegram/123"
    assert media_descriptions.chat_key("telegram/123") == "telegram/123"
    assert media_descriptions.chat_key(None) == ""
    assert quoted_message.QUOTE_MAX_CHARS >= 1500
