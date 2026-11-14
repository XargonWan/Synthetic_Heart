"""Messages that arrived while the Telegram user account was offline are recovered.

Telethon keeps no update state across restarts (StringSession), so anything sent
while the process was down is never pushed to us. At startup the recent chats
are re-read and what was never stored is processed. The "already handled" test
is our own chat history; Telegram's read/unread flags must play no part.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from interface.telegram import telegram as tg

CHAT = -1003098886330
_ids = itertools.count(5000)


def _raw(age: float, text: str = "riprova ora", *, out: bool = False) -> Any:
    ident = next(_ids)
    return SimpleNamespace(
        id=ident,
        message_id=ident,
        chat_id=CHAT,
        chat=SimpleNamespace(id=CHAT, type="supergroup", username=None),
        is_topic_message=False,
        reply_to_message=None,
        date=datetime.now(timezone.utc) - timedelta(seconds=age),
        message=text,
        text=text,
        caption=None,
        out=out,
        thread_id=None,
        from_user=SimpleNamespace(id=31321637, full_name="Jay Cheshire"),
    )


class _Client:
    """Just enough of a Telethon client; exposes read state it must never use."""

    def __init__(self, messages: list[Any], dialog_age: float = 30) -> None:
        self._messages = messages
        self._dialog_date = datetime.now(timezone.utc) - timedelta(seconds=dialog_age)

    async def iter_dialogs(self, limit: int = 0):  # type: ignore[no-untyped-def]
        yield SimpleNamespace(
            entity=object(),
            date=self._dialog_date,
            unread_count=0,  # read on another device: must make no difference
            unread_mentions_count=0,
        )

    async def iter_messages(self, entity: Any, limit: int = 0):  # type: ignore[no-untyped-def]
        for message in sorted(self._messages, key=lambda m: m.date, reverse=True):
            yield message


@pytest.fixture()
def rig(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"live": [], "history": [], "stored": set()}

    async def wrap(bot: Any, raw: Any) -> Any:
        return raw

    async def handle_message(update: Any, context: Any) -> None:
        state["live"].append(update.message)

    async def add_to_context(**kwargs: Any) -> None:
        state["history"].append(kwargs)

    async def exists(path: str, sender_id: str, timestamp: datetime) -> bool:
        return (path, sender_id, timestamp) in state["stored"]

    monkeypatch.setattr(tg, "wrap_message", wrap)
    monkeypatch.setattr(tg, "handle_message", handle_message)
    monkeypatch.setattr(tg, "_backlog_max_age", lambda: 120.0)
    monkeypatch.setattr(tg, "_recovery_window", lambda: 3600.0)
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context", add_to_context
    )
    monkeypatch.setattr("core.chat_history_cache.chat_message_exists", exists)
    tg._seen_messages.clear()
    return state


def _recover(messages: list[Any], **kw: Any) -> int:
    return asyncio.run(tg.recover_missed_messages(_Client(messages, **kw), object()))  # type: ignore[arg-type]


def test_a_missed_message_is_processed_even_if_telegram_says_it_is_read(
    rig: dict[str, Any],
) -> None:
    missed = _raw(40)

    assert _recover([missed]) == 1
    assert rig["live"] == [missed]


def test_a_message_already_in_the_history_is_not_processed_again(
    rig: dict[str, Any],
) -> None:
    done = _raw(40)
    rig["stored"].add((f"telegram/{CHAT}", "31321637", done.date))

    assert _recover([done]) == 0
    assert rig["live"] == [] and rig["history"] == []


def test_only_the_missing_messages_are_processed_oldest_first(
    rig: dict[str, Any],
) -> None:
    first, done, last = _raw(300, "uno"), _raw(200, "due"), _raw(100, "tre")
    rig["stored"].add((f"telegram/{CHAT}", "31321637", done.date))

    assert _recover([last, done, first]) == 2
    # 300 s and 100 s: the first is past the backlog age, the last is live.
    assert [h["message_text"] for h in rig["history"]] == ["uno"]
    assert rig["live"] == [last]


def test_an_old_missed_message_becomes_history_only(rig: dict[str, Any]) -> None:
    stale = _raw(1800, "stamattina")

    assert _recover([stale]) == 1
    assert rig["live"] == []
    assert rig["history"][0]["message_text"] == "stamattina"


def test_our_own_and_media_only_messages_are_skipped(rig: dict[str, Any]) -> None:
    own = _raw(20, "Eccomi!", out=True)
    voice = _raw(20, "")

    assert _recover([own, voice]) == 0
    assert rig["live"] == [] and rig["history"] == []


def test_a_chat_idle_beyond_the_window_is_not_read(rig: dict[str, Any]) -> None:
    assert _recover([_raw(40)], dialog_age=7200) == 0


def test_the_window_can_be_switched_off(
    rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tg, "_recovery_window", lambda: 0.0)
    assert _recover([_raw(40)]) == 0


def test_a_message_delivered_live_and_by_recovery_runs_once(
    rig: dict[str, Any],
) -> None:
    same = _raw(10)

    asyncio.run(tg._on_new_message(object(), SimpleNamespace(message=same)))  # type: ignore[arg-type]

    assert _recover([same]) == 0
    assert rig["live"] == [same]


class _Cursor:
    def __init__(self, row: Any, seen: list[Any]) -> None:
        self._row, self._seen = row, seen

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self._seen.append((sql, params))

    async def fetchone(self) -> Any:
        return self._row


class _Conn:
    def __init__(self, row: Any, seen: list[Any]) -> None:
        self._row, self._seen = row, seen

    async def __aenter__(self) -> "_Conn":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self._row, self._seen)


@pytest.mark.parametrize(("row", "expected"), [((1,), True), (None, False)])
def test_chat_message_exists_matches_path_sender_and_time(
    monkeypatch: pytest.MonkeyPatch, row: Any, expected: bool
) -> None:
    from core import chat_history_cache as cache

    seen: list[Any] = []
    monkeypatch.setattr(cache, "get_conn_ctx", lambda: _Conn(row, seen))
    when = datetime(2026, 11, 14, 20, 57, 40, tzinfo=timezone.utc)

    assert asyncio.run(cache.chat_message_exists("telegram/1", 7, when)) is expected  # type: ignore[arg-type]
    assert seen[0][1] == ("telegram/1", "7", when)


def test_chat_message_exists_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import chat_history_cache as cache

    def boom() -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(cache, "get_conn_ctx", boom)
    when = datetime(2026, 11, 14, 20, 57, 40, tzinfo=timezone.utc)

    # Unknown means "not stored": handling a message twice beats losing it.
    assert asyncio.run(cache.chat_message_exists("telegram/1", "7", when)) is False
