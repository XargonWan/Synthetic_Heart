"""Call turns in the queue: flags kept through coalescing, voice replies."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


import core.message_queue as mq
from core.voice_reply import is_voice_reply


def _entry(**attrs: Any) -> dict[str, Any]:
    return {"message": SimpleNamespace(text="x", **attrs)}


def test_coalescing_keeps_the_call_and_voice_flags() -> None:
    batch = [
        _entry(),  # an earlier message with no flags at all
        _entry(_call=True, call_id="c1", request_tts=True, is_voice_input=True),
    ]
    merged = mq._carry_turn_flags(SimpleNamespace(text="a\nb"), batch)
    assert merged._call is True and merged.call_id == "c1"
    assert merged.request_tts is True and merged.is_voice_input is True


def test_coalescing_leaves_plain_chat_unflagged() -> None:
    merged = mq._carry_turn_flags(SimpleNamespace(text="a"), [_entry(), _entry()])
    assert not getattr(merged, "_call", False)
    assert not getattr(merged, "request_tts", False)


def test_coalescing_keeps_the_latest_screen_frame() -> None:
    old = [{"filename": "old.jpg"}]
    new = [{"filename": "new.jpg"}]
    merged = mq._carry_turn_flags(
        SimpleNamespace(text="a"), [_entry(attachments=old), _entry(attachments=new)]
    )
    assert merged.attachments == new


def test_is_voice_reply() -> None:
    assert is_voice_reply({"call": {"kind": "audio"}})
    assert is_voice_reply({"request_tts": True})
    assert is_voice_reply({"is_voice_input": True})
    assert not is_voice_reply({"interface_path": "x/1"})
    assert not is_voice_reply(None)
