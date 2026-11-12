"""History Telegram delivers late is kept as history, never answered.

When the user account is added to a chat, Telegram sends the latest ~100 messages
(and, after downtime, whatever was missed). They arrive as ordinary incoming
messages but carry their original, old timestamp. They must land in the chat
history under that timestamp and must not reach the live pipeline.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from interface.telegram import telegram as tg


def _message(
    age_seconds: float,
    text: str = "ciao",
    *,
    reply_to: Any = None,
    thread_id: int | None = None,
) -> Any:
    return SimpleNamespace(
        date=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        text=text,
        caption=None,
        chat_id=-1003098886330,
        chat=SimpleNamespace(id=-1003098886330, type="supergroup", username=None),
        thread_id=thread_id,
        is_topic_message=thread_id is not None,
        message_id=77,
        from_user=SimpleNamespace(id=31321637, full_name="Jay Cheshire"),
        reply_to_message=reply_to,
    )


@pytest.mark.parametrize(
    ("age", "backlog"),
    [(0, False), (3, False), (60, False), (119, False), (600, True), (86400, True)],
)
def test_only_old_messages_are_backlog(age: float, backlog: bool) -> None:
    assert tg.is_backlog_message(_message(age)) is backlog


def test_a_naive_timestamp_is_read_as_utc() -> None:
    message = _message(600)
    message.date = message.date.replace(tzinfo=None)
    assert tg.is_backlog_message(message) is True


def test_a_message_without_a_date_is_live() -> None:
    message = _message(0)
    message.date = None
    assert tg.is_backlog_message(message) is False


def test_zero_turns_the_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tg, "_backlog_max_age", lambda: 0.0)
    assert tg.is_backlog_message(_message(86400)) is False


def test_the_limit_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tg, "_backlog_max_age", lambda: 10.0)
    assert tg.is_backlog_message(_message(30)) is True
    assert tg.is_backlog_message(_message(5)) is False


@pytest.fixture()
def pipeline(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Record what reaches the history and what reaches the live handlers."""
    seen: dict[str, list[Any]] = {"history": [], "live": [], "commands": []}

    async def wrap(bot: Any, raw: Any) -> Any:
        return raw

    async def handle_message(update: Any, context: Any) -> None:
        seen["live"].append(update.message)

    async def handle_command(update: Any, context: Any) -> None:
        seen["commands"].append(update.message)

    async def add_to_context(**kwargs: Any) -> None:
        seen["history"].append(kwargs)

    monkeypatch.setattr(tg, "wrap_message", wrap)
    monkeypatch.setattr(tg, "handle_message", handle_message)
    monkeypatch.setattr(tg, "handle_command", handle_command)
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context", add_to_context
    )
    monkeypatch.setattr(tg, "_backlog_max_age", lambda: 120.0)
    return seen


def _deliver(message: Any) -> None:
    asyncio.run(tg._on_new_message(object(), SimpleNamespace(message=message)))  # type: ignore[arg-type]


def test_a_backlog_message_goes_to_history_only(pipeline: dict[str, list[Any]]) -> None:
    old = _message(3600, "ieri sera", thread_id=239)

    _deliver(old)

    assert pipeline["live"] == [] and pipeline["commands"] == []
    entry = pipeline["history"][0]
    assert entry["message_text"] == "ieri sera"
    assert entry["interface_path"] == "telegram/-1003098886330/239"
    assert entry["sender_name"] == "Jay Cheshire" and entry["sender_id"] == "31321637"
    assert entry["message_id"] == 77
    # Kept under its own time, so it sits where it belongs in the history.
    assert entry["timestamp"] == old.date.isoformat()


def test_a_backlog_command_is_not_executed(pipeline: dict[str, list[Any]]) -> None:
    _deliver(_message(3600, "/reset"))

    assert pipeline["commands"] == [] and pipeline["live"] == []
    assert pipeline["history"][0]["message_text"] == "/reset"


def test_a_live_message_still_takes_the_normal_path(
    pipeline: dict[str, list[Any]],
) -> None:
    _deliver(_message(2, "ehi Rekku"))
    _deliver(_message(2, "/help"))

    assert len(pipeline["live"]) == 1
    assert len(pipeline["commands"]) == 1
    assert pipeline["history"] == []  # the live path stores its own history


def test_a_whole_backlog_burst_is_history_in_order(
    pipeline: dict[str, list[Any]],
) -> None:
    for index in range(100):
        _deliver(_message(7200 - index, f"msg {index}"))

    assert pipeline["live"] == []
    assert [e["message_text"] for e in pipeline["history"]] == [
        f"msg {i}" for i in range(100)
    ]


def test_a_backlog_reply_keeps_what_it_answered(pipeline: dict[str, list[Any]]) -> None:
    original = SimpleNamespace(
        message_id=70,
        text="la domanda",
        caption=None,
        from_user=SimpleNamespace(full_name="Ada"),
    )
    _deliver(_message(3600, "la risposta", reply_to=original))

    meta = pipeline["history"][0]["metadata"]
    assert meta == {
        "reply_to": {"sender_name": "Ada", "text": "la domanda", "message_id": 70}
    }


def test_a_backlog_message_without_text_or_sender_is_skipped(
    pipeline: dict[str, list[Any]],
) -> None:
    _deliver(_message(3600, ""))
    nameless = _message(3600, "boh")
    nameless.from_user = None
    _deliver(nameless)

    assert pipeline["history"] == [] and pipeline["live"] == []
