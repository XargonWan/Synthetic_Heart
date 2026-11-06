"""A world the vessel LIVES in (a RetroXR room) keeps its chat between sessions, until the player clears it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


def _interface(monkeypatch: pytest.MonkeyPatch, connectors: dict[str, Any], sessions: dict[str, str]):
    from core import chat_context_manager, chat_history_cache
    from interface import vessel_interface as vi

    cleared: list[tuple[str, str]] = []

    async def _clear_history(path: str) -> None:
        cleared.append(("history", path))

    def _clear_context(path: str) -> None:
        cleared.append(("context", path))

    class _Manager:
        async def end_session(self, *_a: Any, **_k: Any) -> None:
            return None

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(chat_history_cache, "clear_chat_history", _clear_history)
    monkeypatch.setattr(chat_context_manager, "clear_chat_context", _clear_context)
    monkeypatch.setattr(vi, "get_vessel_session_manager", lambda: _Manager())
    iface = vi.VesselInterface.__new__(vi.VesselInterface)
    iface._sessions = dict(sessions)
    iface._connector_environments = lambda: connectors  # type: ignore[method-assign]
    iface._log_activity = _noop  # type: ignore[method-assign]
    return iface, cleared


@pytest.mark.asyncio
async def test_a_session_end_keeps_the_chat_of_a_world_that_remembers(monkeypatch: pytest.MonkeyPatch) -> None:
    iface, cleared = _interface(
        monkeypatch, {"retroxr": SimpleNamespace(remembers_chat=True)}, {"s1": "vessel/retroxr"}
    )
    await iface.end_session("s1", reason="logout")
    assert cleared == [], "her chat is still there on the next summon"


@pytest.mark.asyncio
async def test_a_session_end_still_purges_the_chat_of_a_visited_world(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: the default is unchanged (Minecraft's last run must not leak into the next)."""
    iface, cleared = _interface(
        monkeypatch, {"minecraft": SimpleNamespace(remembers_chat=False)}, {"s1": "vessel/minecraft/w"}
    )
    await iface.end_session("s1", reason="logout")
    assert ("history", "vessel/minecraft/w") in cleared and ("context", "vessel/minecraft/w") in cleared


@pytest.mark.asyncio
async def test_a_connector_with_no_flag_is_a_visited_world(monkeypatch: pytest.MonkeyPatch) -> None:
    iface, cleared = _interface(monkeypatch, {"minecraft": SimpleNamespace()}, {"s1": "vessel/minecraft"})
    await iface.end_session("s1", reason="logout")
    assert ("history", "vessel/minecraft") in cleared


@pytest.mark.asyncio
async def test_forget_clears_every_scope_of_the_world_both_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    iface, cleared = _interface(monkeypatch, {}, {"s1": "vessel/retroxr", "s2": "vessel/minecraft/w"})
    assert await iface.forget_chat("retroxr", None) is True
    assert ("history", "vessel/retroxr") in cleared and ("context", "vessel/retroxr") in cleared
    assert not any("minecraft" in p for _, p in cleared), "another world's chat is not the player's to clear here"


@pytest.mark.asyncio
async def test_forget_clears_the_scope_even_with_no_session_running(monkeypatch: pytest.MonkeyPatch) -> None:
    iface, cleared = _interface(monkeypatch, {}, {})
    assert await iface.forget_chat("retroxr", None) is True
    assert ("history", "vessel/retroxr") in cleared
