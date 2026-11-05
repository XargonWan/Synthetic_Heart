"""Editing an interface's config in the WebUI must restart that interface.

Covers the generic in-place restart used by interfaces without their own
``reload_interface``, Discord's ``stop()``/re-open, and Telegram's reload
actually starting the bot again.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import core.core_initializer as ci
from core.core_initializer import core_initializer


class _FakeInterface:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def stop(self) -> None:
        self.events.append("stop")

    async def start(self) -> None:
        self.events.append("start")


@pytest.mark.asyncio
async def test_restart_in_place_stops_then_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeInterface()
    monkeypatch.setitem(ci.INTERFACE_REGISTRY, "fake_bot", fake)
    monkeypatch.setattr(core_initializer, "_is_interface_enabled", lambda _n: True)

    await core_initializer._restart_interface_in_place("fake_bot")
    await asyncio.sleep(0)  # let the start() task run

    assert fake.events == ["stop", "start"]


@pytest.mark.asyncio
async def test_restart_in_place_leaves_a_disabled_interface_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeInterface()
    monkeypatch.setitem(ci.INTERFACE_REGISTRY, "fake_bot", fake)
    monkeypatch.setattr(core_initializer, "_is_interface_enabled", lambda _n: False)

    await core_initializer._restart_interface_in_place("fake_bot")
    await asyncio.sleep(0)

    assert fake.events == []


@pytest.mark.asyncio
async def test_restart_in_place_ignores_unknown_interface() -> None:
    await core_initializer._restart_interface_in_place("no_such_interface")


def test_reload_handler_is_registered_for_interface_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.config_manager import config_registry

    class _NoReloadInterface(_FakeInterface):
        pass

    monkeypatch.setitem(ci.INTERFACE_REGISTRY, "no_reload_bot", _NoReloadInterface())
    monkeypatch.setattr(config_registry, "_reload_handlers", {})
    core_initializer._register_reload_handlers()

    assert "no_reload_bot" in config_registry._reload_handlers


# ------------------------------------------------------------------ Discord


class _FakeDiscordClient:
    def __init__(self, closed: bool) -> None:
        self._closed = closed
        self.cleared = 0

    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True

    def clear(self) -> None:
        self._closed = False
        self.cleared += 1


def _discord_interface(client: Any) -> Any:
    from interface.discord_interface import DiscordInterface

    iface = object.__new__(DiscordInterface)
    iface.client = client
    return iface


@pytest.mark.asyncio
async def test_discord_stop_closes_the_client() -> None:
    client = _FakeDiscordClient(closed=False)
    await _discord_interface(client).stop()
    assert client.is_closed()


@pytest.mark.asyncio
async def test_discord_stop_without_client_is_a_noop() -> None:
    await _discord_interface(None).stop()


@pytest.mark.asyncio
async def test_discord_start_reopens_a_closed_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import interface.discord_interface as di

    client = _FakeDiscordClient(closed=True)
    iface = _discord_interface(client)
    started: list[bool] = []

    async def _fake_start_client() -> None:
        started.append(client.is_closed())

    monkeypatch.setattr(di, "get_discord_token", lambda: "token")
    monkeypatch.setattr(iface, "_register_trainer_id", lambda: None, raising=False)
    monkeypatch.setattr(iface, "_start_discord_client", _fake_start_client)

    await iface.start()

    assert client.cleared == 1
    assert started == [False]


# ----------------------------------------------------------------- Telegram


@pytest.mark.asyncio
async def test_telegram_reload_stops_recreates_and_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import interface.telegram_bot as tb

    events: list[str] = []

    class _NewInterface:
        async def start(self) -> None:
            events.append("start")

    async def _stop_bot() -> None:
        events.append("stop_bot")

    def _initialize() -> _NewInterface:
        events.append("initialize")
        return _NewInterface()

    monkeypatch.setattr(tb, "stop_bot", _stop_bot)
    monkeypatch.setattr(tb, "initialize_interface", _initialize)
    monkeypatch.setattr(core_initializer, "_is_interface_enabled", lambda _n: True)

    await tb.reload_interface()

    assert events == ["stop_bot", "initialize", "start"]


@pytest.mark.asyncio
async def test_telegram_reload_does_not_start_a_disabled_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import interface.telegram_bot as tb

    events: list[str] = []

    class _NewInterface:
        async def start(self) -> None:
            events.append("start")

    async def _stop_bot() -> None:
        events.append("stop_bot")

    monkeypatch.setattr(tb, "stop_bot", _stop_bot)
    monkeypatch.setattr(tb, "initialize_interface", lambda: _NewInterface())
    monkeypatch.setattr(core_initializer, "_is_interface_enabled", lambda _n: False)

    await tb.reload_interface()

    assert events == ["stop_bot"]
