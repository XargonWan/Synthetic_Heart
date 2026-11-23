"""A private chat with a bot account must not become an endless exchange.

Live (2026-11-12): the synth, writing as a Telegram user account, messaged a bot
(Pilgram). The bot answers everything and a DM is always "directed", so each
reply started a turn that wrote again. The ward judges the incoming messages in
core: bots rank below people, and a bot that sends too much is paused (its
messages stay as history, they start no turn).
"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from core import bot_ward

PILGRAM = "telegram/5551234567"


def _msg(
    *, bot: bool = True, username: str | None = "Pilgram_bot", chat_id: int = 5551234567
) -> Any:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=chat_id, is_bot=bot, username=username),
        chat_id=chat_id,
    )


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str, str]]]:
    """Fresh ward state, no database: the system events are captured."""
    bot_ward.reset()
    events: list[tuple[str, str, str]] = []

    async def fake_record(
        kind: str, component: str | None = None, detail: str | None = None, **_: Any
    ) -> None:
        events.append((kind, component or "", detail or ""))

    monkeypatch.setattr("core.system_events.record_event", fake_record)
    clock = {"now": 1000.0}
    monkeypatch.setattr(bot_ward, "_now", lambda: clock["now"])
    monkeypatch.setattr(bot_ward, "_clock", clock, raising=False)
    yield events
    bot_ward.reset()


def advance(seconds: float) -> None:
    bot_ward._clock["now"] += seconds  # type: ignore[attr-defined]


def check(path: str = PILGRAM, message: Any = None) -> bot_ward.Verdict:
    return asyncio.run(bot_ward.check_inbound(path, message or _msg()))


# -- who is a bot, what is a private chat -------------------------------------


@pytest.mark.parametrize(
    ("flag", "username", "path", "expected"),
    [
        (True, "anyone", "discord_bot/42", True),
        (False, "Pilgram_bot", "telegram/1", True),
        (False, "PilgramBot", "telegram_bot/1", True),
        (False, "pilgrambot", "telegram/1", True),
        (False, "Pilgram", "telegram/1", False),
        (False, "robot_fan", "telegram/1", False),
        (False, "RoBot", "synth_webui/webui_default", False),  # rule is Telegram's
        (False, None, "telegram/1", False),
    ],
)
def test_who_counts_as_a_bot(
    flag: bool, username: str | None, path: str, expected: bool
) -> None:
    assert bot_ward.is_bot_sender(_msg(bot=flag, username=username), path) is expected


def test_a_message_without_a_sender_is_not_a_bot() -> None:
    assert (
        bot_ward.is_bot_sender(SimpleNamespace(from_user=None), "telegram/1") is False
    )


@pytest.mark.parametrize(
    ("path", "private"),
    [
        ("telegram/5551234567", True),
        ("telegram_bot/5551234567", True),
        ("discord_bot/42", True),
        ("telegram/-1003098886330", False),
        ("telegram/-1003098886330/239", False),
        ("discord_bot/1/2", False),
        ("telegram", False),
        ("", False),
    ],
)
def test_only_one_to_one_chats_are_in_scope(path: str, private: bool) -> None:
    assert bot_ward.is_private_path(path) is private


# -- the verdicts ---------------------------------------------------------------


def test_a_human_is_never_touched() -> None:
    verdict = check(message=_msg(bot=False, username="Jay"))
    assert verdict.action == bot_ward.ALLOW and verdict.priority_drop == 0


def test_a_bot_in_a_group_is_left_to_peer_policy() -> None:
    verdict = check("telegram/-1003098886330", _msg(chat_id=-1003098886330))
    assert verdict.action == bot_ward.ALLOW


def test_a_bot_message_is_accepted_one_band_below_ordinary_chat() -> None:
    verdict = check()
    assert verdict.action == bot_ward.DEMOTE
    assert verdict.priority_drop == 1


def test_ten_messages_pass_and_the_eleventh_starts_the_pause(
    clean: list[tuple[str, str, str]],
) -> None:
    verdicts = [check().action for _ in range(10)]
    assert verdicts == [bot_ward.DEMOTE] * 10

    assert check().action == bot_ward.HISTORY_ONLY
    assert clean == [
        ("bot_ward", PILGRAM, "for 30 min after 10 messages in 10 min")
    ]  # recorded once


def test_the_whole_pause_is_history_only_and_logged_once(
    clean: list[tuple[str, str, str]],
) -> None:
    for _ in range(11):
        check()
    for _ in range(5):
        advance(60)
        assert check().action == bot_ward.HISTORY_ONLY

    assert len(clean) == 1


def test_the_pause_ends_and_the_count_restarts() -> None:
    for _ in range(11):
        check()

    advance(1801)

    assert check().action == bot_ward.DEMOTE
    assert [check().action for _ in range(9)] == [bot_ward.DEMOTE] * 9
    assert check().action == bot_ward.HISTORY_ONLY  # a fresh 10-message budget


def test_a_slow_bot_never_trips_it() -> None:
    for _ in range(40):
        advance(120)  # 5 per window
        assert check().action == bot_ward.DEMOTE


def test_old_messages_fall_out_of_the_window() -> None:
    for _ in range(9):
        check()
    advance(601)
    assert [check().action for _ in range(10)] == [bot_ward.DEMOTE] * 10


def test_conversations_are_independent() -> None:
    for _ in range(11):
        check()

    other_bot = check("telegram/777", _msg(chat_id=777, username="Other_bot"))
    human = check("telegram/888", _msg(bot=False, username="Ada", chat_id=888))

    assert other_bot.action == bot_ward.DEMOTE
    assert human.action == bot_ward.ALLOW


def test_the_thresholds_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_ward, "_cfg_int", _fixed({"BOT_WARD_MAX_MESSAGES": 2}))
    assert [check().action for _ in range(3)] == [
        bot_ward.DEMOTE,
        bot_ward.DEMOTE,
        bot_ward.HISTORY_ONLY,
    ]


def test_it_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_ward, "_enabled", lambda: False)
    assert {check().action for _ in range(30)} == {bot_ward.ALLOW}


def _fixed(overrides: dict[str, int]) -> Any:
    original = bot_ward._cfg_int

    def read(key: str, minimum: int = 0) -> int:
        return overrides.get(key, original(key, minimum))

    return read


# -- what the queue does with the verdict --------------------------------------


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> Any:
    import core.message_queue as mq

    class _Q:
        def __init__(self) -> None:
            self.items: deque[Any] = deque()

        def empty(self) -> bool:
            return not self.items

        def get_nowait(self) -> Any:
            return self.items.popleft()

        async def put(self, item: Any) -> None:
            self.items.append(item)

    class _Plugin:
        def get_rate_limit(self) -> tuple[float, int, float]:
            return 10**9, 1, 1.0

    async def not_blocked(user_id: Any) -> bool:
        return False

    fake = _Q()
    monkeypatch.setattr(mq, "is_user_blocked", not_blocked)
    monkeypatch.setattr("core.plugin_instance.get_plugin", lambda: _Plugin())
    monkeypatch.setattr(mq, "_get_queue", lambda: fake)
    monkeypatch.setattr("core.rate_limit.is_allowed", lambda *a, **k: True)
    monkeypatch.setattr(mq, "get_reaction_emoji", lambda: "")
    monkeypatch.setattr(mq, "ensure_message_user_fields", lambda m: None)
    return fake


def _queued(message: Any) -> None:
    import core.message_queue as mq

    asyncio.run(
        mq.enqueue(None, message, interface_id="telegram", skip_mention_check=True)
    )


def _chat_message(*, bot: bool, chat_id: int) -> Any:
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=chat_id, is_bot=bot, username="Pilgram_bot" if bot else "Jay"
        ),
        chat=SimpleNamespace(
            id=chat_id, type="private", title=None, username=None, first_name=None
        ),
        text="hello",
        chat_id=chat_id,
    )


def test_a_bot_ranks_one_band_below_a_person(queue: Any) -> None:
    import core.message_queue as mq

    _queued(_chat_message(bot=False, chat_id=1))
    _queued(_chat_message(bot=True, chat_id=5551234567))

    person, bot = queue.items
    assert person[0] == mq._heap_key(mq.PRIORITY_GENERAL)
    assert bot[0] == mq._heap_key(mq.PRIORITY_AMBIENT)
    # Still above the background band: it is answered, just after the people.
    assert mq.PRIORITY_AMBIENT > mq.PRIORITY_BACKGROUND_THRESHOLD


def test_a_paused_bot_starts_no_turn_but_other_chats_still_do(queue: Any) -> None:
    for _ in range(10):
        _queued(_chat_message(bot=True, chat_id=5551234567))
    assert len(queue.items) == 10

    _queued(_chat_message(bot=True, chat_id=5551234567))  # the 11th: paused
    _queued(_chat_message(bot=False, chat_id=42))  # a person elsewhere

    assert len(queue.items) == 11  # only the person was added


def test_a_paused_bot_resolves_the_waiting_future(queue: Any) -> None:
    import core.message_queue as mq

    async def scenario() -> Any:
        for _ in range(10):
            await mq.enqueue(
                None,
                _chat_message(bot=True, chat_id=5551234567),
                interface_id="telegram",
                skip_mention_check=True,
            )
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await mq.enqueue(
            None,
            _chat_message(bot=True, chat_id=5551234567),
            interface_id="telegram",
            skip_mention_check=True,
            response_future=future,
        )
        return future.done(), future.result()

    assert asyncio.run(scenario()) == (True, None)


# -- what the model is told -----------------------------------------------------


def test_the_system_event_reads_naturally() -> None:
    from core.system_events import _event_text

    text = _event_text(
        {
            "kind": "bot_ward",
            "component": PILGRAM,
            "detail": "for 30 min after 10 messages in 10 min",
        }
    )
    assert text == (
        "conversation with bot telegram/5551234567 paused "
        "for 30 min after 10 messages in 10 min"
    )


def _prompt_source(*, bot: bool, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from datetime import datetime, timezone

    from core.prompt_engine import build_json_prompt

    async def no_injections(message: Any, ctx: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr("core.action_parser.gather_static_injections", no_injections)
    message = SimpleNamespace(
        chat_id=5551234567,
        text="ok",
        message_id=1,
        from_user=SimpleNamespace(
            id=5551234567,
            full_name="Pilgram" if bot else "Jay",
            username="Pilgram_bot" if bot else "Jay",
            is_bot=bot,
        ),
        date=datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc),
        interface_path=PILGRAM,
    )
    result = asyncio.run(build_json_prompt(message, {}, interface_name="telegram"))
    return result["input"]["payload"]["source"]


def test_the_model_is_told_the_sender_is_a_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _prompt_source(bot=True, monkeypatch=monkeypatch)

    assert source["is_bot"] is True
    assert source["note"] == bot_ward.BOT_NOTE


def test_a_person_gets_no_bot_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _prompt_source(bot=False, monkeypatch=monkeypatch)

    assert "is_bot" not in source and "note" not in source
