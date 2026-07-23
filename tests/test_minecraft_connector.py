"""Tests for the Minecraft Vessel connector (mock HTTP, no real bridge)."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from plugins.vessel_base import PerceptionEvent, VesselActionResult, WorldState
from plugins.vessels.minecraft_connector import (
    CONNECTOR_CLASS,
    ENVIRONMENT,
    MinecraftConnector,
)
from core.vessel_registry import VESSEL_REGISTRY


def test_connector_self_registers() -> None:
    assert "minecraft" in VESSEL_REGISTRY.get_available_connectors()
    assert CONNECTOR_CLASS is MinecraftConnector


def test_describe_capabilities() -> None:
    caps = MinecraftConnector().describe_capabilities()
    assert caps["movement"] is True
    assert caps["chat"] is True
    assert caps["local"] is True


def test_resolve_base_url_from_settings() -> None:
    conn = MinecraftConnector()
    url = conn._resolve_base_url({"bridge_host": "10.0.0.5", "bridge_port": 9000})
    assert url == "http://10.0.0.5:9000"


def test_resolve_base_url_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = MinecraftConnector()
    monkeypatch.setattr(
        "plugins.vessels.minecraft_connector.config_registry.get_value",
        lambda key, default=None: None,
    )
    url = conn._resolve_base_url({})
    assert url == "http://127.0.0.1:8137"


def test_dispatch_event_builds_perception_event() -> None:
    conn = MinecraftConnector()
    received: List[PerceptionEvent] = []
    conn._on_event = received.append
    conn._dispatch_event(
        {
            "event_type": "chat",
            "summary": "Steve: hi",
            "actor": "Steve",
            "salience": 0.7,
            "data": {"message": "hi"},
        }
    )
    assert len(received) == 1
    ev = received[0]
    assert isinstance(ev, PerceptionEvent)
    assert ev.environment == ENVIRONMENT
    assert ev.event_type == "chat"
    assert ev.actor == "Steve"
    assert ev.data == {"message": "hi"}


def test_dispatch_event_without_callback_is_noop() -> None:
    conn = MinecraftConnector()
    conn._on_event = None
    # Should not raise.
    conn._dispatch_event({"event_type": "chat", "summary": "x"})


def test_dispatch_event_ignores_non_dict() -> None:
    conn = MinecraftConnector()
    received: List[PerceptionEvent] = []
    conn._on_event = received.append
    conn._dispatch_event("not a dict")  # type: ignore[arg-type]
    assert received == []


@pytest.mark.asyncio
async def test_act_when_not_connected_returns_error() -> None:
    conn = MinecraftConnector()
    res = await conn.act("say", {"text": "hi"})
    assert isinstance(res, VesselActionResult)
    assert res.ok is False
    assert "not connected" in (res.detail or "")


@pytest.mark.asyncio
async def test_get_world_state_when_not_connected_returns_none() -> None:
    conn = MinecraftConnector()
    assert await conn.get_world_state() is None


@pytest.mark.asyncio
async def test_act_posts_cmd_when_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = MinecraftConnector()
    conn._connected = True
    captured: Dict[str, Any] = {}

    async def _fake_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "detail": "done", "data": {"x": 1}}

    monkeypatch.setattr(conn, "_post", _fake_post)
    res = await conn.act("move", {"x": 10})
    assert captured["path"] == "/cmd"
    assert captured["payload"] == {"action": "move", "payload": {"x": 10}}
    assert res.ok is True
    assert res.detail == "done"
    assert res.data == {"x": 1}


@pytest.mark.asyncio
async def test_get_world_state_when_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = MinecraftConnector()
    conn._connected = True

    async def _fake_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ok": True,
            "data": {
                "health": 18.0,
                "position": {"x": 1, "y": 2, "z": 3},
                "connected": True,
                "username": "Synth",
            },
        }

    monkeypatch.setattr(conn, "_post", _fake_post)
    state = await conn.get_world_state()
    assert isinstance(state, WorldState)
    assert state.environment == ENVIRONMENT
    assert state.health == 18.0
    assert state.flags["connected"] is True
    assert state.extra["username"] == "Synth"
