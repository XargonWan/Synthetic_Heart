"""Tests for the Minecraft Vessel connector (mock HTTP, no real bridge)."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from plugins.rift_vessel.vessel_base import (
    PerceptionEvent,
    VesselActionResult,
    WorldState,
)
from plugins.rift_vessel.minecraft.minecraft import (
    CONNECTOR_CLASS,
    ENVIRONMENT,
    MinecraftConnector,
    MinecraftVesselPlugin,
    PLUGIN_CLASS,
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
        "plugins.rift_vessel.minecraft.minecraft.config_registry.get_value",
        lambda key, default=None, **kwargs: None,
    )
    url = conn._resolve_base_url({})
    assert url == "http://127.0.0.1:8137"


def test_resolve_server_target_from_override() -> None:
    target = MinecraftConnector._resolve_server_target(
        {"host": "play.example.com", "port": "25565"}
    )
    assert target == {"host": "play.example.com", "port": 25565}


def test_resolve_server_target_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_get(key: str, default: Any = None, **kwargs: Any) -> Any:
        return default

    monkeypatch.setattr(
        "plugins.rift_vessel.minecraft.minecraft.config_registry.get_value",
        _fake_get,
    )
    target = MinecraftConnector._resolve_server_target({})
    assert target == {"host": "127.0.0.1", "port": 44383}


def test_resolve_server_target_remaps_loopback_in_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loopback host is auto-remapped to the Docker host gateway in a container."""
    monkeypatch.setattr(
        "plugins.rift_vessel.minecraft.minecraft._is_in_container",
        lambda: True,
    )
    for loopback in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        target = MinecraftConnector._resolve_server_target(
            {"host": loopback, "port": 25565}
        )
        assert target == {"host": "host.docker.internal", "port": 25565}


def test_resolve_server_target_no_remap_on_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside a container the loopback host is preserved as-is."""
    monkeypatch.setattr(
        "plugins.rift_vessel.minecraft.minecraft._is_in_container",
        lambda: False,
    )
    target = MinecraftConnector._resolve_server_target(
        {"host": "127.0.0.1", "port": 25565}
    )
    assert target == {"host": "127.0.0.1", "port": 25565}


def test_resolve_server_target_no_remap_for_non_loopback_in_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real LAN/remote host is never remapped, even inside a container."""
    monkeypatch.setattr(
        "plugins.rift_vessel.minecraft.minecraft._is_in_container",
        lambda: True,
    )
    target = MinecraftConnector._resolve_server_target(
        {"host": "192.168.1.13", "port": 44383}
    )
    assert target == {"host": "192.168.1.13", "port": 44383}


def test_resolve_server_target_no_version_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no configured version, the target omits the key (auto-detect)."""

    def _fake_get(key: str, default: Any = None, **kwargs: Any) -> Any:
        return default

    monkeypatch.setattr(
        "plugins.rift_vessel.minecraft.minecraft.config_registry.get_value",
        _fake_get,
    )
    target = MinecraftConnector._resolve_server_target({})
    assert "version" not in target


def test_resolve_server_target_version_from_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-connect version override pins the protocol version in the target."""

    def _fake_get(key: str, default: Any = None, **kwargs: Any) -> Any:
        return default

    monkeypatch.setattr(
        "plugins.rift_vessel.minecraft.minecraft.config_registry.get_value",
        _fake_get,
    )
    target = MinecraftConnector._resolve_server_target(
        {"host": "play.example.com", "port": 25565, "version": "1.21.4"}
    )
    assert target == {"host": "play.example.com", "port": 25565, "version": "1.21.4"}


def test_resolve_server_target_version_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured MINECRAFT_SERVER_VERSION is applied when no override is set."""

    def _fake_get(key: str, default: Any = None, **kwargs: Any) -> Any:
        if key == "MINECRAFT_SERVER_VERSION":
            return "1.20.6"
        return default

    monkeypatch.setattr(
        "plugins.rift_vessel.minecraft.minecraft.config_registry.get_value",
        _fake_get,
    )
    target = MinecraftConnector._resolve_server_target({})
    assert target["version"] == "1.20.6"


async def test_dispatch_event_builds_perception_event() -> None:
    conn = MinecraftConnector()
    received: List[PerceptionEvent] = []
    conn._on_event = received.append
    await conn._dispatch_event(
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


async def test_dispatch_event_awaits_async_callback() -> None:
    conn = MinecraftConnector()
    received: List[PerceptionEvent] = []

    async def _async_cb(event: PerceptionEvent) -> None:
        received.append(event)

    conn._on_event = _async_cb
    await conn._dispatch_event({"event_type": "chat", "summary": "Steve: hi"})
    assert len(received) == 1
    assert received[0].event_type == "chat"


async def test_dispatch_event_without_callback_is_noop() -> None:
    conn = MinecraftConnector()
    conn._on_event = None
    # Should not raise.
    await conn._dispatch_event({"event_type": "chat", "summary": "x"})


async def test_dispatch_event_ignores_non_dict() -> None:
    conn = MinecraftConnector()
    received: List[PerceptionEvent] = []
    conn._on_event = received.append
    await conn._dispatch_event("not a dict")  # type: ignore[arg-type]
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


def _mock_skin_config(monkeypatch: pytest.MonkeyPatch, values: Dict[str, Any]) -> None:
    def _get_value(key: str, default: Any = None, **kwargs: Any) -> Any:
        return values.get(key, default)

    monkeypatch.setattr(
        "plugins.rift_vessel.minecraft.minecraft.config_registry.get_value",
        _get_value,
    )


@pytest.mark.asyncio
async def test_apply_skin_file_builds_command_with_explicit_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = MinecraftConnector()
    captured: Dict[str, Any] = {}

    async def _fake_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "detail": "skin command sent", "data": {}}

    _mock_skin_config(
        monkeypatch,
        {
            "MINECRAFT_SKIN_FILE": "/config/storage/MINECRAFT_SKIN_FILE/skin.png",
            "MINECRAFT_SKIN_MODEL": "slim",
            "MINECRAFT_SKIN_PUBLIC_BASE_URL": "http://synth.local:8080",
            "MINECRAFT_SKIN_COMMAND_TEMPLATE": "/skin url {url} {model}",
        },
    )
    monkeypatch.setattr(conn, "_post", _fake_post)
    await conn._apply_skin()
    assert captured["path"] == "/cmd"
    assert captured["payload"]["action"] == "skin"
    assert (
        captured["payload"]["payload"]["command"]
        == "/skin url http://synth.local:8080/api/config/MINECRAFT_SKIN_FILE/file slim"
    )


@pytest.mark.asyncio
async def test_apply_skin_file_default_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = MinecraftConnector()
    captured: Dict[str, Any] = {}

    async def _fake_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        captured["payload"] = payload
        return {"ok": True, "detail": "skin command sent", "data": {}}

    _mock_skin_config(
        monkeypatch,
        {
            "MINECRAFT_SKIN_FILE": "/config/storage/MINECRAFT_SKIN_FILE/skin.png",
            "MINECRAFT_SKIN_PUBLIC_BASE_URL": "http://synth.local:8080/",
        },
    )
    monkeypatch.setattr(conn, "_post", _fake_post)
    await conn._apply_skin()
    assert captured["payload"]["action"] == "skin"
    assert (
        captured["payload"]["payload"]["command"]
        == "/skin url http://synth.local:8080/api/config/MINECRAFT_SKIN_FILE/file"
    )


@pytest.mark.asyncio
async def test_apply_skin_no_file_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = MinecraftConnector()
    called = False

    async def _fake_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal called
        called = True
        return {"ok": True}

    _mock_skin_config(monkeypatch, {"MINECRAFT_SKIN_FILE": ""})
    monkeypatch.setattr(conn, "_post", _fake_post)
    await conn._apply_skin()
    assert called is False


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
    assert "respawn" in state.possible_actions


# ---------------------------------------------------------------------------
# Connection-failure reason propagation (so Synth can tell the requester WHY).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_records_last_error_on_bridge_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = MinecraftConnector()

    async def _fake_ensure() -> None:
        return None

    async def _fake_get(path: str) -> Dict[str, Any]:
        return {"ok": True, "mineflayer": True}

    async def _fake_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": False, "detail": "No data available for version 26.2"}

    monkeypatch.setattr(conn, "_ensure_bridge_running", _fake_ensure)
    monkeypatch.setattr(conn, "_get", _fake_get)
    monkeypatch.setattr(conn, "_post", _fake_post)

    ok = await conn.connect({"host": "srv.example", "port": 25565}, lambda e: None)
    assert ok is False
    assert conn.last_error is not None
    assert "No data available for version 26.2" in conn.last_error
    assert "srv.example:25565" in conn.last_error


@pytest.mark.asyncio
async def test_connect_records_last_error_on_health_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = MinecraftConnector()

    async def _fake_ensure() -> None:
        return None

    async def _fake_get(path: str) -> Dict[str, Any]:
        return {"ok": False, "detail": "connection refused"}

    monkeypatch.setattr(conn, "_ensure_bridge_running", _fake_ensure)
    monkeypatch.setattr(conn, "_get", _fake_get)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    ok = await conn.connect({}, lambda e: None)
    assert ok is False
    assert conn.last_error is not None
    assert "connection refused" in conn.last_error


@pytest.mark.asyncio
async def test_connect_records_last_error_on_missing_mineflayer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = MinecraftConnector()

    async def _fake_ensure() -> None:
        return None

    async def _fake_get(path: str) -> Dict[str, Any]:
        return {"ok": True, "mineflayer": False}

    monkeypatch.setattr(conn, "_ensure_bridge_running", _fake_ensure)
    monkeypatch.setattr(conn, "_get", _fake_get)

    ok = await conn.connect({}, lambda e: None)
    assert ok is False
    assert conn.last_error is not None
    assert "mineflayer" in conn.last_error


async def _no_sleep(*_args: Any, **_kwargs: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# Minecraft as a separate, attachable Vessel sub-plugin (Grillo-style).
# ---------------------------------------------------------------------------


def test_minecraft_vessel_plugin_is_exported() -> None:
    assert PLUGIN_CLASS is MinecraftVesselPlugin


def test_minecraft_vessel_plugin_metadata() -> None:
    meta = MinecraftVesselPlugin().get_metadata()
    assert meta["name"] == "minecraft_vessel"
    assert meta["display_name"] == "Minecraft Vessel"
    assert meta["category"] == "Vessels"
    assert meta["icon"] == "icon.svg"
    assert meta["guide"] == "guide.md"


def test_minecraft_vessel_plugin_registers_no_actions() -> None:
    plugin = MinecraftVesselPlugin()
    assert plugin.get_supported_actions() == {}
    assert plugin.get_supported_action_types() == []
