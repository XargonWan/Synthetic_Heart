"""Queue-time merging of pending messages that share the exact same interface path."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import core.message_queue as mq

PATH = "vessel/minecraft/192_168_1_42_25565"


def _item(
    text: str,
    ts: float,
    *,
    path: str = PATH,
    interface: str = "vessel",
    player_chat: bool = True,
    message_id: Any = None,
    from_user: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    message = SimpleNamespace(
        text=text,
        message_id=message_id,
        from_user=from_user,
        chat_id=path,
        interface_path=path,
        thread_id=None,
        chat=None,
        reply_to_message=None,
        date=None,
        _vessel_player_chat=player_chat,
        _no_compact=player_chat,
    )
    return {
        "message": message,
        "chat_id": path,
        "thread_id": None,
        "interface": interface,
        "interface_path": path,
        "timestamp": ts,
        "context": None,
        "priority": False,
        "no_compact": player_chat,
        "vessel_player_chat": player_chat,
        "call": False,
        **extra,
    }


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> Any:
    fresh: Any = asyncio.PriorityQueue()
    monkeypatch.setattr(mq, "_queue", fresh)
    monkeypatch.setattr(mq, "_get_queue", lambda: fresh)
    monkeypatch.setattr(mq, "_coalesce_settings", lambda: (True, 20))
    return fresh


async def _enqueue(
    queue: Any,
    item: dict[str, Any],
    prio: int = mq.PRIORITY_HIGH,
) -> bool:
    """Mirror the tail of ``enqueue``: merge when possible, otherwise put."""
    if mq._try_coalesce_pending(item, prio):
        return True
    await queue.put((mq._heap_key(prio), len(queue._queue) + 1, item))
    return False


@pytest.mark.asyncio
async def test_pending_items_of_the_same_path_become_one_element(queue) -> None:
    """Six messages pile up behind a running turn: they leave one pending element."""
    # The turn in flight already left the heap, so it is never a merge target.
    merged = [await _enqueue(queue, _item(f"line {n}", 2.0 + n)) for n in range(6)]
    assert merged == [False, True, True, True, True, True]
    assert len(queue._queue) == 1

    (pending,) = mq.snapshot_queue()
    assert pending["merged_count"] == 6
    assert pending["enqueued_at"] == 2.0
    assert pending["last_enqueued_at"] == 7.0
    assert str(pending["text_preview"]).startswith("line 0 ⏎ line 1")


@pytest.mark.asyncio
async def test_a_path_that_is_only_similar_is_not_merged(queue) -> None:
    await _enqueue(queue, _item("a", 1.0))
    other = _item("b", 2.0, path="vessel/minecraft/192_168_1_42_25566")
    assert await _enqueue(queue, other) is False
    assert len(queue._queue) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"call": True, "call_id": "c1"},
        {"priority": True},
        {"vessel_reflection": True},
        {"vessel_appraisal": True},
        {"vessel_idle_beat": True},
        {"response_future": object()},
        {"media_future": object()},
    ],
)
async def test_items_with_their_own_lifecycle_are_never_merged(
    queue, extra: dict[str, Any]
) -> None:
    await _enqueue(queue, _item("a", 1.0))
    assert await _enqueue(queue, _item("b", 2.0, **extra)) is False
    assert len(queue._queue) == 2


@pytest.mark.asyncio
async def test_player_chat_never_merges_with_a_perception(queue) -> None:
    await _enqueue(queue, _item("chat", 1.0))
    perception = _item("sighting", 2.0, player_chat=False, no_compact=False)
    assert await _enqueue(queue, perception) is False


@pytest.mark.asyncio
async def test_different_interface_thread_or_band_are_not_merged(queue) -> None:
    await _enqueue(queue, _item("a", 1.0, interface="telegram_bot", player_chat=False))
    assert (
        await _enqueue(queue, _item("b", 2.0, interface="discord", player_chat=False))
        is False
    )
    assert (
        await _enqueue(
            queue,
            _item("c", 3.0, interface="telegram_bot", player_chat=False, thread_id=7),
        )
        is False
    )
    assert (
        await _enqueue(
            queue,
            _item("d", 4.0, interface="telegram_bot", player_chat=False),
            prio=mq.PRIORITY_TRAINER,
        )
        is False
    )
    assert len(queue._queue) == 4


@pytest.mark.asyncio
async def test_ordinary_chat_of_the_same_path_is_merged(queue) -> None:
    kwargs: dict[str, Any] = {
        "interface": "telegram_bot",
        "player_chat": False,
        "path": "tg/1",
    }
    await _enqueue(queue, _item("hi", 1.0, **kwargs), prio=mq.PRIORITY_GENERAL)
    assert await _enqueue(queue, _item("there", 2.0, **kwargs), mq.PRIORITY_GENERAL)
    assert len(queue._queue) == 1


@pytest.mark.asyncio
async def test_cap_starts_a_new_element(queue, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mq, "_coalesce_settings", lambda: (True, 2))
    results = [await _enqueue(queue, _item(f"m{n}", float(n))) for n in range(5)]
    # base + 2 absorbed, then a second base that takes the last one.
    assert results == [False, True, True, False, True]
    assert len(queue._queue) == 2


@pytest.mark.asyncio
async def test_disabled_setting_keeps_every_element(
    queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mq, "_coalesce_settings", lambda: (False, 20))
    await _enqueue(queue, _item("a", 1.0))
    assert await _enqueue(queue, _item("b", 2.0)) is False
    assert len(queue._queue) == 2


@pytest.mark.asyncio
async def test_duplicate_message_id_is_dropped(queue) -> None:
    await _enqueue(queue, _item("a", 1.0, message_id=5))
    assert await _enqueue(queue, _item("a", 2.0, message_id=5)) is True
    only = queue._queue[0][2]
    assert not only.get("absorbed")


@pytest.mark.asyncio
async def test_consumer_side_expansion_keeps_the_player_chat_contract(queue) -> None:
    base = _item("Xargon: ciao", 1.0)
    base["context"] = {"vessel_world_state": {"hp": 20}}
    later = _item("Xargon: sei qui?", 2.0)
    later["context"] = {"vessel_world_state": {"hp": 18}}
    await _enqueue(queue, base)
    await _enqueue(queue, later)

    popped = queue._queue[0][2]
    batch = mq._flatten_absorbed([popped])
    assert [e["message"].text for e in batch] == ["Xargon: ciao", "Xargon: sei qui?"]
    assert "absorbed" not in popped

    merged = mq._build_merged_message(batch, popped["message"])
    assert merged.text == "Xargon: ciao\nXargon: sei qui?"
    # No bogus "group" speaker: vessel text already carries its speaker.
    assert merged.from_user is None
    assert merged._vessel_player_chat is True
    assert merged._no_compact is True
    assert merged.interface_path == PATH


def test_merge_with_senders_still_uses_the_group_user() -> None:
    user = SimpleNamespace(id=1, username="ann", full_name="Ann")
    first = _item(
        "hi", 1.0, interface="telegram_bot", player_chat=False, from_user=user
    )
    second = _item(
        "yo", 2.0, interface="telegram_bot", player_chat=False, from_user=user
    )
    merged = mq._build_merged_message([first, second], first["message"])
    assert merged.from_user.username == "group"
    assert merged.text.count("\n") == 1
