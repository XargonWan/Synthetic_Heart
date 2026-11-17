"""The Activities card must name the world and log each visit once."""

from __future__ import annotations

from typing import Any

import pytest


def _interface(monkeypatch: pytest.MonkeyPatch, live_id: str = "s1"):
    from interface import vessel_interface as vi

    ended: list[str] = []
    logged: list[dict[str, Any]] = []

    class _Manager:
        async def start_session(self, environment: str, path: str) -> str:
            return live_id

        async def end_session(self, session_id: str, reason: str = "") -> None:
            ended.append(session_id)

    async def _log(**kwargs: Any) -> None:
        logged.append(kwargs)

    monkeypatch.setattr(vi, "get_vessel_session_manager", lambda: _Manager())
    iface = vi.VesselInterface.__new__(vi.VesselInterface)
    iface._sessions = {}
    iface._connector_environments = lambda: {}
    iface._log_activity = _log
    return iface, ended, logged


@pytest.mark.asyncio
async def test_a_reused_session_is_not_logged_as_a_second_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    iface, _, logged = _interface(monkeypatch)
    await iface.begin_session("minecraft", world="192.168.1.42:25565")
    await iface.begin_session("minecraft", world="192.168.1.42:25565")
    assert [row["event_type"] for row in logged] == ["session_start"]


@pytest.mark.asyncio
async def test_ending_an_already_closed_session_logs_no_unknown_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    iface, ended, logged = _interface(monkeypatch)
    await iface.begin_session("minecraft", world="w")
    await iface.end_session("s1", reason="disconnected")  # the 30 s grace close
    await iface.end_session("s1", reason="connect_failed")  # the late rollback
    events = [(row["event_type"], row["environment"]) for row in logged]
    assert events == [("session_start", "minecraft"), ("session_end", "minecraft")]
    assert ended == ["s1", "s1"], "the manager end stays idempotent and is still asked"
