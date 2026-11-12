"""The synth shows "typing…", "recording…" and "sending a file…" while it works."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator

import pytest

from core import chat_activity as ca
from core import core_initializer as ci

PATH = "fake_chat/42"


class _Chat:
    """An interface that can show activity and remembers every pulse."""

    def __init__(self, fail: bool = False) -> None:
        self.pulses: list[tuple[str, str]] = []
        self.fail = fail

    async def send_activity(self, interface_path: str, kind: str) -> None:
        self.pulses.append((interface_path, kind))
        if self.fail:
            raise RuntimeError("client gone")


@pytest.fixture()
def chat(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Chat]:
    iface = _Chat()
    saved_registry = dict(ci.INTERFACE_REGISTRY)
    saved_active = list(ci.core_initializer.active_interfaces)
    ci.INTERFACE_REGISTRY["fake_chat"] = iface
    ci.core_initializer.active_interfaces.append("fake_chat")
    monkeypatch.setattr(ca, "REFRESH_SECONDS", 0.01)
    ca._pulses.clear()
    try:
        yield iface
    finally:
        ca._pulses.clear()
        ci.INTERFACE_REGISTRY.clear()
        ci.INTERFACE_REGISTRY.update(saved_registry)
        ci.core_initializer.active_interfaces[:] = saved_active


async def _settle(seconds: float = 0.08) -> None:
    await asyncio.sleep(seconds)


def test_typing_repeats_until_it_is_ended(chat: _Chat) -> None:
    async def scenario() -> tuple[int, int]:
        await ca.begin(PATH)
        await _settle()
        during = len(chat.pulses)
        await ca.end(PATH)
        after_end = len(chat.pulses)
        await _settle()
        return during, len(chat.pulses) - after_end

    during, after = asyncio.run(scenario())
    assert during >= 2  # kept alive, not a single pulse
    assert after == 0  # and silent once ended
    assert {kind for _p, kind in chat.pulses} == {"typing"}


def test_begin_is_idempotent_per_conversation(chat: _Chat) -> None:
    async def scenario() -> int:
        await ca.begin(PATH)
        await ca.begin(PATH)
        count = len(ca._pulses)
        await ca.end(PATH)
        return count

    assert asyncio.run(scenario()) == 1


def test_an_interface_that_cannot_show_activity_is_ignored(chat: _Chat) -> None:
    async def scenario() -> int:
        ci.INTERFACE_REGISTRY["mute"] = SimpleNamespace()
        ci.core_initializer.active_interfaces.append("mute")
        await ca.begin("mute/1")
        await ca.begin("synth_webui/webui_default")
        await ca.begin("grillo/-1")
        await ca.begin(None)
        return len(ca._pulses)

    assert asyncio.run(scenario()) == 0


def test_an_interface_that_is_switched_off_shows_nothing(chat: _Chat) -> None:
    ci.core_initializer.active_interfaces.remove("fake_chat")

    async def scenario() -> int:
        await ca.begin(PATH)
        await _settle(0.03)
        return len(chat.pulses)

    assert asyncio.run(scenario()) == 0


def test_the_option_can_turn_it_off(
    chat: _Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ca, "_enabled", lambda: False)

    async def scenario() -> int:
        await ca.begin(PATH)
        await _settle(0.03)
        return len(chat.pulses)

    assert asyncio.run(scenario()) == 0


def test_a_phase_switches_the_wording_and_hands_it_back(chat: _Chat) -> None:
    async def scenario() -> list[str]:
        await ca.begin(PATH)
        async with ca.show(PATH, ca.KIND_RECORD_AUDIO):
            during = ca._pulses[PATH].kind
        back = ca._pulses[PATH].kind
        await ca.end(PATH)
        return [during, back]

    assert asyncio.run(scenario()) == ["record_audio", "typing"]


def test_a_phase_on_its_own_starts_and_stops_the_indicator(chat: _Chat) -> None:
    async def scenario() -> tuple[bool, bool]:
        async with ca.show(PATH, ca.KIND_UPLOAD_FILE):
            inside = PATH in ca._pulses
            await _settle(0.03)
        return inside, PATH in ca._pulses

    inside, after = asyncio.run(scenario())
    assert inside is True and after is False
    assert ("fake_chat/42", "upload_file") in chat.pulses


def test_it_gives_up_after_repeated_failures(chat: _Chat) -> None:
    chat.fail = True

    async def scenario() -> tuple[int, bool]:
        await ca.begin(PATH)
        await _settle(0.15)
        return len(chat.pulses), PATH in ca._pulses

    pulses, still_running = asyncio.run(scenario())
    assert pulses == ca.MAX_FAILURES
    assert still_running is False


def test_it_never_outlives_the_cap(
    chat: _Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ca, "MAX_SECONDS", 0.05)

    async def scenario() -> bool:
        await ca.begin(PATH)
        await _settle(0.2)
        return PATH in ca._pulses

    assert asyncio.run(scenario()) is False


@pytest.mark.parametrize(
    ("action_type", "payload", "expected"),
    [
        ("tts_speak", {"text": "hi"}, "record_audio"),
        ("send_message", {"text": "hi", "media": ["a.png"]}, "upload_file"),
        ("send_message", {"text": "hi"}, None),
        ("update_emotion_state", {}, None),
        ("send_message", None, None),
    ],
)
def test_which_actions_get_their_own_wording(
    action_type: str, payload: Any, expected: str | None
) -> None:
    assert ca.kind_for_action(action_type, payload) == expected


# -- the action wrapper in core.action_parser ---------------------------------


def test_a_delivered_message_stops_the_typing_indicator(chat: _Chat) -> None:
    from core.action_parser import _action_activity

    async def scenario() -> tuple[bool, bool]:
        await ca.begin(PATH)
        async with _action_activity(
            "send_message", {"text": "hi"}, {"interface_path": PATH}
        ):
            running = PATH in ca._pulses
        return running, PATH in ca._pulses

    running, after = asyncio.run(scenario())
    assert running is True and after is False


def test_a_voice_note_shows_recording_then_stops(chat: _Chat) -> None:
    from core.action_parser import _action_activity

    async def scenario() -> str:
        await ca.begin(PATH)
        async with _action_activity(
            "tts_speak", {"text": "hi"}, {"interface_path": PATH}
        ):
            kind = ca._pulses[PATH].kind
        assert PATH not in ca._pulses
        return kind

    assert asyncio.run(scenario()) == "record_audio"


def test_an_unrelated_action_leaves_the_indicator_alone(chat: _Chat) -> None:
    from core.action_parser import _action_activity

    async def scenario() -> bool:
        await ca.begin(PATH)
        async with _action_activity(
            "update_emotion_state", {}, {"interface_path": PATH}
        ):
            pass
        stays = PATH in ca._pulses
        await ca.end(PATH)
        return stays

    assert asyncio.run(scenario()) is True


# -- the interfaces' own wording ------------------------------------------------


class _FakeChatBot:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_chat_action(self, *args: Any, **kwargs: Any) -> bool:
        names = ("chat_id", "action")
        self.calls.append({**dict(zip(names, args)), **kwargs})
        return True


@pytest.mark.parametrize(
    ("kind", "action"),
    [
        ("typing", "typing"),
        ("record_audio", "record_voice"),
        ("upload_audio", "upload_voice"),
        ("upload_file", "upload_document"),
        ("something_new", "typing"),
    ],
)
def test_telegram_user_account_maps_kinds_and_forum_topics(
    kind: str, action: str
) -> None:
    from interface.telegram.telegram import TelegramUserInterface

    iface = TelegramUserInterface.__new__(TelegramUserInterface)
    fake = _FakeChatBot()
    iface.bot = fake

    asyncio.run(iface.send_activity("telegram/-1003098886330/239", kind))

    call = fake.calls[0]
    assert call["chat_id"] == "-1003098886330"
    assert call["action"] == action
    assert call["message_thread_id"] == 239


def test_telegram_bot_maps_kinds_and_forum_topics() -> None:
    from interface.telegram_bot.telegram_bot import TelegramInterface

    iface = TelegramInterface.__new__(TelegramInterface)
    fake = _FakeChatBot()
    iface.bot = fake

    asyncio.run(iface.send_activity("telegram_bot/-1003098886330/239", "record_audio"))
    asyncio.run(iface.send_activity("telegram_bot/31321637", "typing"))

    first, second = fake.calls
    assert first["chat_id"] == -1003098886330
    assert first["action"] == "record_voice" and first["message_thread_id"] == 239
    assert second["chat_id"] == 31321637 and second["message_thread_id"] is None


def test_an_interface_without_a_client_does_nothing() -> None:
    from interface.telegram.telegram import TelegramUserInterface

    iface = TelegramUserInterface.__new__(TelegramUserInterface)
    iface.bot = None
    asyncio.run(iface.send_activity("telegram/1", "typing"))  # must not raise


def test_telethon_facade_passes_the_forum_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from interface.telegram._compat import TelethonBot

    requests: list[Any] = []

    class _Client:
        async def get_input_entity(self, peer: Any) -> Any:
            return ("input", peer)

        async def __call__(self, request: Any) -> None:
            requests.append(request)

    bot = TelethonBot(_Client())
    asyncio.run(bot.send_chat_action(-100123, "record_voice", message_thread_id=239))

    request = requests[0]
    assert type(request.action).__name__ == "SendMessageRecordAudioAction"
    assert request.top_msg_id == 239


async def _unused() -> AsyncIterator[None]:  # keeps AsyncIterator import honest
    yield
