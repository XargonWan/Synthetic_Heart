"""Tests for the RetroXR Rift Vessel connector, its socket and the body assets.

No DB, no LLM, no RetroXR: the mod is played by a fake WebSocket / the FastAPI
TestClient. Each case is a way the rift could wrongly work (see the mutations in
the commit message).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.rift_vessel import embodiment_assets
from plugins.rift_vessel.retroxr.api import PROTOCOL_VERSION, create_retroxr_router
from plugins.rift_vessel.retroxr.retroxr import (
    CORE_VERBS,
    WORLD_VERBS,
    RetroXRConnector,
)
from plugins.rift_vessel.vessel_base import PerceptionEvent, VesselActionResult


class FakeSocket:
    """Stands in for the mod's WebSocket on the connector side."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


async def _connected(hello: dict[str, Any] | None = None) -> tuple[RetroXRConnector, FakeSocket, list]:
    events: list[PerceptionEvent] = []
    conn = RetroXRConnector()
    ws = FakeSocket()
    conn.attach_socket(ws, hello or {"room": "den", "capabilities": ["motion"]})
    assert await conn.connect({}, events.append) is True
    return conn, ws, events


# -- connector ---------------------------------------------------------------


async def test_connect_refused_while_the_rift_is_closed() -> None:
    conn = RetroXRConnector()
    assert await conn.connect({}, lambda e: None) is False
    assert conn.is_connected is False
    assert "Summoner" in (conn.last_error or "")


async def test_action_round_trip() -> None:
    conn, ws, _ = await _connected()

    task = asyncio.create_task(conn.act("say", {"text": "hi"}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    frame = ws.sent[-1]
    assert frame["type"] == "action" and frame["action"] == "say"
    assert frame["payload"] == {"text": "hi", "audio": True}, "in RetroXR a say is always spoken"

    await conn.handle_frame(
        {"type": "action_result", "id": frame["id"], "ok": True, "detail": "spoken", "data": {"n": 1}}
    )
    res = await task
    assert res == VesselActionResult(ok=True, detail="spoken", data={"n": 1})


async def test_results_are_matched_by_id_not_order() -> None:
    conn, ws, _ = await _connected({"room": "den", "verbs": ["look_at", "touch"]})
    t1 = asyncio.create_task(conn.act("look_at", {"target": "tv"}))
    t2 = asyncio.create_task(conn.act("touch", {"target": "lamp"}))
    await asyncio.sleep(0.01)
    ids = {f["action"]: f["id"] for f in ws.sent}
    # answer the SECOND request first
    await conn.handle_frame({"type": "action_result", "id": ids["touch"], "ok": False, "detail": "out_of_reach"})
    await conn.handle_frame({"type": "action_result", "id": ids["look_at"], "ok": True})
    assert (await t1).ok is True
    assert (await t2).detail == "out_of_reach"


async def test_body_never_plays_unknown_verbs_are_refused() -> None:
    conn, ws, _ = await _connected()
    for verb in ("press_button", "play", "set_input", "attack", "respawn", ""):
        res = await conn.act(verb, {})
        assert res.ok is False and "unknown_verb" in (res.detail or ""), verb
    assert ws.sent == [], "a refused verb must never reach the room"


async def test_every_declared_verb_is_a_known_one() -> None:
    conn, ws, _ = await _connected({"room": "den", "verbs": sorted(CORE_VERBS | set(WORLD_VERBS))})
    assert "attack" not in CORE_VERBS
    for verb in list(CORE_VERBS | set(WORLD_VERBS)):
        task = asyncio.create_task(conn.act(verb, {"target": "x"}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await conn.handle_frame({"type": "action_result", "id": ws.sent[-1]["id"], "ok": True})
        assert (await task).ok is True, verb


async def test_closing_the_rift_fails_waiting_actions() -> None:
    conn, _, _ = await _connected()
    task = asyncio.create_task(conn.act("move", {"target": "tv"}))
    await asyncio.sleep(0.01)
    conn.detach_socket()
    res = await asyncio.wait_for(task, timeout=1)
    assert res.ok is False and res.detail == "rift_closed"
    assert conn.is_connected is False
    # and nothing is sent once it is closed
    assert (await conn.act("say", {"text": "x"})).detail == "rift_closed"


async def test_a_dead_socket_marks_the_connector_down() -> None:
    conn, ws, _ = await _connected()

    async def boom(_: dict[str, Any]) -> None:
        raise RuntimeError("socket gone")

    ws.send_json = boom  # type: ignore[method-assign]
    res = await conn.act("say", {"text": "x"})
    assert res.detail == "rift_closed"
    assert conn.is_connected is False
    assert "socket gone" in (conn.last_error or "")


async def test_events_become_perceptions() -> None:
    conn, _, events = await _connected()
    await conn.handle_frame(
        {"type": "event", "event_type": "touch", "summary": "the player touched your hand",
         "actor": "player", "salience": 0.9, "data": {"part": "hand"}}
    )
    assert len(events) == 1
    ev = events[0]
    assert (ev.environment, ev.event_type, ev.actor, ev.salience) == ("retroxr", "touch", "player", 0.9)
    assert ev.data == {"part": "hand"}


async def test_state_frame_feeds_world_state() -> None:
    conn, _, _ = await _connected({"room": "den"})
    assert (await conn.get_world_state()).position is None
    await conn.handle_frame(
        {"type": "state", "state": {"position": {"x": 1, "y": 0, "z": 2}, "holding": "cart",
                                    "extra": {"entities": [{"name": "tv"}]}}}
    )
    st = await conn.get_world_state()
    assert st.environment == "retroxr"
    assert st.position == {"x": 1, "y": 0, "z": 2}
    assert st.flags["holding"] == "cart"
    assert st.extra["entities"] == [{"name": "tv"}] and st.extra["room"] == "den"
    assert "say" in st.possible_actions and "attack" not in st.possible_actions


async def test_world_identity_is_the_room() -> None:
    conn, _, _ = await _connected({"room": "xenu.mod:attic"})
    assert conn.get_world_identity() == "xenu.mod:attic"


async def test_disconnect_closes_the_socket() -> None:
    conn, ws, _ = await _connected()
    await conn.disconnect()
    assert ws.closed is True and conn.is_connected is False


# -- body assets -------------------------------------------------------------


def _skin(root: Path, name: str, clips: dict[str, list[str]]) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "model.vrm").write_bytes(b"vrm")
    for folder, files in clips.items():
        d = root / name / "animations" / folder
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).write_bytes(b"fbx")


async def test_animation_lookup_order(tmp_path: Path) -> None:
    _skin(tmp_path, "Rei", {"greet": ["Wave.fbx"], "vessel/walk": ["WalkRei.fbx"]})
    _skin(tmp_path, "Mine", {"vessel/walk": ["WalkMine.fbx"], "vessel/sit": ["Sit.fbx"]})

    # the skin's own vessel clip beats the fallback skin's
    assert embodiment_assets.resolve_animation("walk", "Mine", tmp_path)["clips"][0]["file"] == "WalkMine.fbx"
    # a state folder is found when there is no vessel clip
    got = embodiment_assets.resolve_animation("greet", "Mine", tmp_path)
    assert got["skin"] == "Rei" and got["namespace"] == "state"
    # nothing anywhere
    assert embodiment_assets.resolve_animation("moonwalk", "Mine", tmp_path) is None


async def test_the_skins_folder_is_the_repositorys(tmp_path: Path) -> None:
    # It was `parent.parent` of core/ once, and moving the module under plugins/ silently pointed it at plugins/skins.
    assert embodiment_assets.SKINS_ROOT.name == "skins"
    assert (embodiment_assets.SKINS_ROOT.parent / "plugins" / "rift_vessel").is_dir(), "its parent is the repository root"


async def test_a_loose_clip_with_the_word_in_its_name_is_found_last(tmp_path: Path) -> None:
    # Rei's skin ships "Texting And Walking.fbx" at the top of animations/, with no folder of its own.
    base = tmp_path / "Rei" / "animations"
    base.mkdir(parents=True)
    (base / "Texting And Walking.fbx").write_bytes(b"x")
    (base / "Thankful.fbx").write_bytes(b"x")
    got = embodiment_assets.resolve_animation("walk", "Rei", tmp_path)
    assert got is not None and got["namespace"] == "loose" and got["clips"][0]["file"] == "Texting And Walking.fbx"
    assert embodiment_assets.resolve_animation("thank", "Rei", tmp_path)["clips"][0]["file"] == "Thankful.fbx"
    assert embodiment_assets.resolve_animation("moon", "Rei", tmp_path) is None, "a word must START with the name"
    assert embodiment_assets.resolve_animation("ing", "Rei", tmp_path) is None, "not a piece from the middle of one"
    # a folder or a vessel clip still wins over a loose file
    (base / "vessel" / "walk").mkdir(parents=True)
    (base / "vessel" / "walk" / "Proper.fbx").write_bytes(b"x")
    assert embodiment_assets.resolve_animation("walk", "Rei", tmp_path)["clips"][0]["file"] == "Proper.fbx"


async def test_animation_names_cannot_climb_out_of_the_skins(tmp_path: Path) -> None:
    _skin(tmp_path, "Rei", {"idle": ["Idle.fbx"]})
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "x.fbx").write_bytes(b"x")
    for bad in ("..", "../secret", "a/b", "..\\secret", ""):
        assert embodiment_assets.resolve_animation(bad, "Rei", tmp_path) is None, bad
    assert embodiment_assets.resolve_animation("idle", "../Rei", tmp_path) is None


async def test_descriptor_travels_with_the_clip(tmp_path: Path) -> None:
    _skin(tmp_path, "Rei", {"vessel/sit": ["Sit.fbx"]})
    desc = {"fps": 30, "loop": {"start_frame": 1, "end_frame": 9}}
    (tmp_path / "Rei" / "animations" / "vessel" / "sit" / "Sit.fbx.json").write_text(json.dumps(desc))
    clip = embodiment_assets.resolve_animation("sit", "Rei", tmp_path)["clips"][0]
    assert clip["url"] == "/skins/Rei/animations/vessel/sit/Sit.fbx"
    assert clip["descriptor"] == desc
    assert clip["descriptor_url"].endswith("Sit.fbx.json")


async def test_describe_assets_names_missing_animations(tmp_path: Path) -> None:
    _skin(tmp_path, "Rei", {"vessel/walk": ["Walk.fbx"]})
    body = await embodiment_assets.describe_assets(["walk", "dance"], skins_root=tmp_path)
    assert body["skin"] == "Rei"
    assert body["model"]["url"] == "/skins/Rei/model.vrm"
    assert list(body["animations"]) == ["walk"]
    assert body["missing_animations"] == ["dance"]
    assert body["manifest_url"] == "/api/karada/assets/manifest"


# -- the socket --------------------------------------------------------------


class FakeVesselPlugin:
    """What the rift handler needs of the Rift Vessel plugin."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.connected: list[str] = []
        self.disconnected: list[str] = []

    async def connect_world(self, connector_name: str | None = None, overrides: Any = None):
        from core.vessel_registry import VESSEL_REGISTRY

        self.connected.append(connector_name or "")
        if not self.ok:
            return VesselActionResult(ok=False, detail="world_unavailable")
        conn = VESSEL_REGISTRY.load_connector(connector_name)
        ok = await conn.connect({}, lambda e: None)
        return VesselActionResult(ok=ok, data={"session_id": "sess-1"})

    async def disconnect_world(self, connector_name: str | None = None):
        from core.vessel_registry import VESSEL_REGISTRY

        self.disconnected.append(connector_name or "")
        await VESSEL_REGISTRY.load_connector(connector_name).disconnect()
        return VesselActionResult(ok=True)


@pytest.fixture
def rift(monkeypatch: pytest.MonkeyPatch):
    from core import core_initializer
    from plugins.rift_vessel.retroxr import api as vessel_retroxr_api
    from core.vessel_registry import VESSEL_REGISTRY

    VESSEL_REGISTRY.unload_connector("retroxr")
    plugin = FakeVesselPlugin()
    monkeypatch.setitem(core_initializer.PLUGIN_REGISTRY, "vessel_plugin", plugin)
    monkeypatch.setattr(vessel_retroxr_api.webui_auth, "configured_token", lambda: None)
    app = FastAPI()
    app.include_router(create_retroxr_router())
    yield TestClient(app), plugin
    VESSEL_REGISTRY.unload_connector("retroxr")


def _hello(**extra: Any) -> str:
    return json.dumps({"type": "hello", "protocol": PROTOCOL_VERSION, "room": "den",
                       "capabilities": ["motion"], "animations": [], **extra})


def test_handshake_welcomes_with_the_body(rift) -> None:
    client, plugin = rift
    with client.websocket_connect("/api/vessel/retroxr/ws") as ws:
        ws.send_text(_hello())
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
        assert welcome["session"] == "sess-1"
        assert "assets" in welcome
    assert plugin.connected == ["retroxr"]
    assert plugin.disconnected == ["retroxr"], "leaving must end the session through the plugin"


@pytest.mark.parametrize(
    "first,code",
    [
        ("not json", "bad_hello"),
        (json.dumps({"type": "event"}), "bad_hello"),
        (json.dumps({"type": "hello", "protocol": 99}), "protocol"),
    ],
)
def test_bad_handshakes_are_refused_with_a_reason(rift, first: str, code: str) -> None:
    client, plugin = rift
    with client.websocket_connect("/api/vessel/retroxr/ws") as ws:
        ws.send_text(first)
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == code
    assert plugin.connected == []


def test_refused_connect_leaves_no_socket_behind(rift) -> None:
    from core.vessel_registry import VESSEL_REGISTRY

    client, plugin = rift
    plugin.ok = False
    with client.websocket_connect("/api/vessel/retroxr/ws") as ws:
        ws.send_text(_hello())
        err = ws.receive_json()
        assert err["code"] == "connect_failed" and "world_unavailable" in err["message"]
    assert VESSEL_REGISTRY.load_connector("retroxr").is_connected is False


def test_a_second_rift_is_refused_while_one_is_open(rift) -> None:
    client, _ = rift
    with client.websocket_connect("/api/vessel/retroxr/ws") as first:
        first.send_text(_hello())
        assert first.receive_json()["type"] == "welcome"
        with client.websocket_connect("/api/vessel/retroxr/ws") as second:
            second.send_text(_hello())
            assert second.receive_json()["code"] == "busy"


def test_summoned_is_a_real_event_not_ambient_telemetry() -> None:
    """Being summoned (and what she sees) is a moment of its own: it must reach Synth as a
    turn, not be folded into the batched ambient perceptions with 'proximity' and 'spawn'."""
    from plugins.rift_vessel.vessel_base import is_ephemeral_event

    assert is_ephemeral_event("summoned") is False
    assert is_ephemeral_event("proximity") is True  # the control: ambient stays ambient


# -- the plugin brings its own socket; the core only offers a place to mount it ---------------------------


async def test_the_vessel_plugin_mounts_the_rift_socket_through_the_webui(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.webui as webui
    from plugins.rift_vessel.vessel_plugin import VesselPlugin

    mounted: list[tuple[Any, str]] = []

    class FakeWebUI:
        def register_plugin_router(self, router: Any, name: str = "") -> None:
            mounted.append((router, name))

    monkeypatch.setattr(webui, "synth_webui_interface", FakeWebUI())
    await VesselPlugin._mount_world_routes(None)  # type: ignore[arg-type]
    assert len(mounted) == 1
    paths = [getattr(r, "path", "") for r in mounted[0][0].routes]
    assert "/api/vessel/retroxr/ws" in paths, "the route the mod connects to is the one that was mounted"


def test_the_webui_mounts_a_plugin_router_without_knowing_the_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.webui import SynthWebUIInterface
    from plugins.rift_vessel.retroxr import api as rift_api

    monkeypatch.setattr(rift_api.webui_auth, "configured_token", lambda: None)

    class Host:
        app = FastAPI()

    SynthWebUIInterface.register_plugin_router(Host(), create_retroxr_router(), "test")  # type: ignore[arg-type]
    with TestClient(Host.app).websocket_connect("/api/vessel/retroxr/ws") as ws:
        ws.send_json({"type": "not-a-hello"})
        assert ws.receive_json()["code"] == "bad_hello", "the mounted socket answers like the rift's own"
