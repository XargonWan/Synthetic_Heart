"""Call turns in the queue: flags kept through coalescing, young-turn interruption."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

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


@pytest.fixture
def _window(monkeypatch: pytest.MonkeyPatch):
    def _set(seconds: float) -> None:
        import core.config_manager as cfg

        monkeypatch.setattr(
            cfg.config_registry,
            "get_value",
            lambda key, default=None, **_: (
                seconds if key == "CALL_TURN_INTERRUPT_WINDOW_SEC" else default
            ),
        )

    return _set


async def _running_turn(call_id: str | None, *, cancellable: bool = True):
    task = asyncio.ensure_future(asyncio.sleep(30))
    mq._register_call_turn({"call_id": call_id}, task, 9, cancellable)
    await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
async def test_a_young_turn_of_the_same_call_is_interrupted(_window) -> None:
    _window(8.0)
    task = await _running_turn("c1")
    try:
        assert mq._interrupt_young_call_turn("c1") is True
        with pytest.raises(asyncio.CancelledError):
            await task
        assert mq._call_turn_interrupted(task)
        # Once interrupted it is not interrupted a second time.
        assert mq._interrupt_young_call_turn("c1") is False
    finally:
        mq._clear_call_turn()


@pytest.mark.asyncio
async def test_other_calls_and_old_turns_are_left_alone(_window) -> None:
    _window(8.0)
    task = await _running_turn("c1")
    try:
        assert mq._interrupt_young_call_turn("other") is False
        assert mq._interrupt_young_call_turn(None) is False
        mq._active_call_turn.started -= 60  # type: ignore[union-attr]
        assert mq._interrupt_young_call_turn("c1") is False
        assert not task.cancelled()
    finally:
        task.cancel()
        mq._clear_call_turn()


@pytest.mark.asyncio
async def test_interruption_can_be_disabled_or_unsafe(_window) -> None:
    _window(0.0)
    task = await _running_turn("c1")
    try:
        assert mq._interrupt_young_call_turn("c1") is False
    finally:
        task.cancel()
        mq._clear_call_turn()

    _window(8.0)
    task = await _running_turn("c1", cancellable=False)  # e.g. a Zen engine
    try:
        assert mq._interrupt_young_call_turn("c1") is False
    finally:
        task.cancel()
        mq._clear_call_turn()


@pytest.mark.asyncio
async def test_a_non_call_turn_is_never_registered() -> None:
    task = await _running_turn(None)
    try:
        assert mq._active_call_turn is None
        assert mq._interrupt_young_call_turn("c1") is False
    finally:
        task.cancel()
