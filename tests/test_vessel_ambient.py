"""Ambient telemetry from a world never starts an LLM turn of its own: it is absorbed into the perception ring and rides
along with the next real prompt. Real events (player chat, a routine's result) still enqueue at once."""

from __future__ import annotations

from typing import Any

import pytest


def _iface(monkeypatch: pytest.MonkeyPatch, ambient_on: bool = False):
    from interface import vessel_interface as vi

    class _Manager:
        async def start_session(self, *_a: Any, **_k: Any) -> str:
            return "s1"

        def __getattr__(self, name: str) -> Any:
            async def _noop(*_a: Any, **_k: Any) -> None:
                return None

            return _noop

    monkeypatch.setattr(vi, "get_vessel_session_manager", lambda: _Manager())
    monkeypatch.setattr(vi, "_ambient_triggers_turn", lambda: ambient_on)
    iface = vi.VesselInterface.__new__(vi.VesselInterface)
    iface._sessions = {}
    iface._pending_perceptions = {}
    iface._recent_signatures = {}
    iface._last_enqueue_by_type = {}
    iface._last_enqueue_at = 0.0
    queued: list[dict[str, Any]] = []

    async def _enqueue(**kw: Any) -> None:
        queued.append(kw)

    async def _log_activity(**_kw: Any) -> None:
        return None

    monkeypatch.setattr(iface, "_enqueue_perception", _enqueue)
    monkeypatch.setattr(iface, "_log_activity", _log_activity, raising=False)
    monkeypatch.setattr(iface, "_tracked_path_for_environment", lambda _e: "vessel/retroxr/root", raising=False)
    return iface, queued


@pytest.mark.asyncio
async def test_a_sighting_starts_no_turn_and_buffers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    iface, queued = _iface(monkeypatch)
    ok = await iface.on_world_event("retroxr", "sighting", "The Cable is no longer there.", world="root")
    assert ok is True
    assert queued == [], "an ambient line started an LLM turn"
    assert iface._pending_perceptions == {}, "it was buffered for a later batch turn"


@pytest.mark.asyncio
async def test_the_ambient_line_is_in_the_ring_for_the_next_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.chat_context_manager import get_perception_memory

    iface, queued = _iface(monkeypatch)
    await iface.on_world_event("retroxr", "sighting", "The Wii has appeared on your left, 2.0 m away.", world="root")
    ring = [str(m) for buf in get_perception_memory().values() for m in buf]
    assert any("The Wii has appeared" in line for line in ring), "the ambient line did not reach the perception ring"
    assert queued == []


@pytest.mark.asyncio
async def test_a_routine_result_still_enqueues_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    iface, queued = _iface(monkeypatch)
    await iface.on_world_event("retroxr", "search_done", "You found the Game Boy.", world="root")
    assert [q["event_type"] for q in queued] == ["search_done"]


@pytest.mark.asyncio
async def test_the_old_batch_comes_back_with_the_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    iface, queued = _iface(monkeypatch, ambient_on=True)
    await iface.on_world_event("retroxr", "sighting", "The Cable is no longer there.", world="root")
    assert queued == []
    assert sum(len(v) for v in iface._pending_perceptions.values()) == 1, "with the switch on, it is buffered as before"
