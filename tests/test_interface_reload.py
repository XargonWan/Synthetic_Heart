"""Editing an interface's config in the WebUI must restart that interface.

Covers the generic in-place restart used by interfaces without their own
``reload_interface``, Telegram's reload
actually starting the bot again.
"""

from __future__ import annotations

import asyncio

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
