"""RetroXR: her body follows Karada (states, voice), she speaks as well as writes, and when
nobody is talking to her she is TOLD what is around her and asked what she wants to do.

No DB, no LLM, no RetroXR: the mod is a fake socket, Karada and Vox are stubs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from core import vessel_beat
from plugins.rift_vessel.karada_bridge import RiftKaradaTransport
from plugins.rift_vessel.retroxr.retroxr import RetroXRConnector
from plugins.rift_vessel.vessel_base import VesselActionResult


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        pass


class FakeKarada:
    """The Karada state server, as far as the connector touches it."""

    def __init__(self, state: str | None = "think") -> None:
        self.transports: list[Any] = []
        self.audio: list[dict[str, Any]] = []
        self._state = state
        self.presence: str | None = None
        self.presence_log: list[str | None] = []

    def add_transport(self, t: Any) -> None:
        if t not in self.transports:
            self.transports.append(t)

    def remove_transport(self, t: Any) -> None:
        if t in self.transports:
            self.transports.remove(t)

    def get_current_animation_state(self) -> dict[str, Any]:
        return {"state": self._state, "descriptor": None, "started_at": None}

    async def broadcast_audio(self, **kw: Any) -> None:
        self.audio.append(kw)

    def get_presence(self) -> str | None:
        return self.presence

    async def set_presence(self, name: str | None, source: str = "") -> None:
        self.presence = name
        self.presence_log.append(name)
        for t in list(self.transports):
            await t.broadcast_presence({"type": "vrm_presence", "state": name or "idle"})


@pytest.fixture
def karada(monkeypatch: pytest.MonkeyPatch) -> FakeKarada:
    import core.animation_handler as ah

    k = FakeKarada()
    monkeypatch.setattr(ah, "get_karada_state_server", lambda: k)
    return k


async def _connected(hello: dict[str, Any] | None = None) -> tuple[RetroXRConnector, FakeSocket]:
    conn = RetroXRConnector()
    ws = FakeSocket()
    conn.attach_socket(ws, hello or {"room": "den", "capabilities": ["motion"]})
    assert await conn.connect({}, lambda e: None) is True
    return conn, ws


# -- Karada: the body does what the avatar does ------------------------------


async def test_the_rift_is_a_karada_transport_while_it_is_open(karada: FakeKarada) -> None:
    conn, ws = await _connected()
    assert conn._karada in karada.transports
    assert ws.sent == [], "nothing goes out before the welcome"
    # after the welcome she is told what she is doing right now, not only the next change
    await conn.announce_state()
    assert ws.sent[0] == {"type": "karada", "kind": "animation",
                          "payload": {"type": "vrm_animation_v2", "state": "think"}}
    await conn.disconnect()
    assert karada.transports == [], "a closed rift must not stay registered (Karada would keep sending to it)"


async def test_a_dropped_socket_also_unregisters(karada: FakeKarada) -> None:
    conn, _ = await _connected()
    conn.detach_socket()
    assert karada.transports == []


async def test_every_kind_of_karada_payload_reaches_the_mod() -> None:
    sent: list[dict[str, Any]] = []
    open_ = [True]

    async def send(frame: dict[str, Any]) -> bool:
        sent.append(frame)
        return True

    t = RiftKaradaTransport(send, lambda: open_[0])
    await t.broadcast_animation({"type": "vrm_animation_v2", "state": "talk", "started_at": 1.0})
    await t.broadcast_audio({"type": "tts-play", "url": "/media/a.wav", "audio_duration_s": 2.0, "text": "ciao"})
    await t.broadcast_face({"type": "vrm_face", "values": {"happy": 0.5}})
    await t.broadcast_model({"type": "vrm_model", "name": "x", "url": "/skins/Rei/model.vrm"})
    await t.broadcast_expression({"type": "vrm_expression_set", "targets": {}})
    await t.send_to_session("s", {"type": "x"})
    assert [f["kind"] for f in sent] == ["animation", "audio", "face", "model", "expression", "direct"]
    assert all(f["type"] == "karada" for f in sent)
    assert sent[1]["payload"]["url"] == "/media/a.wav", "Karada's own payload, unchanged"
    assert t.get_connected_sessions() == ["retroxr"]

    open_[0] = False
    sent.clear()
    await t.broadcast_animation({"state": "idle"})
    assert sent == [] and t.get_connected_sessions() == [], "nothing is sent to a closed rift"


# -- presence: "summon" while she is being introduced -----------------------


async def test_she_is_summoning_from_the_rift_opening_until_she_steps_out(karada: FakeKarada) -> None:
    conn, ws = await _connected()
    assert karada.presence == "summon", "the vessel says she is being introduced as soon as it connects"
    await conn.announce_state()
    assert {"type": "karada", "kind": "presence",
            "payload": {"type": "vrm_presence", "state": "summon"}} in ws.sent, "the mod is told once it listens"
    ws.sent.clear()
    await conn.handle_frame({"type": "event", "event_type": "summoned", "summary": "out", "salience": 1.0})
    assert karada.presence is None, "the mod's `summoned` event ends it"
    assert ws.sent[-1]["payload"] == {"type": "vrm_presence", "state": "idle"}, "and the mod hears it"


async def test_a_rift_closed_mid_arrival_does_not_leave_her_summoning(karada: FakeKarada) -> None:
    conn, _ = await _connected()
    assert karada.presence == "summon"
    await conn.disconnect()
    await asyncio.sleep(0)
    assert karada.presence is None


async def test_karada_presence_reaches_transports_and_a_late_client() -> None:
    from core.animation_handler import KaradaStateServer
    from core.karada_transport import KaradaTransport

    class Spy(RiftKaradaTransport):
        pass

    got: list[dict[str, Any]] = []

    async def send(frame: dict[str, Any]) -> bool:
        got.append(frame)
        return True

    server = KaradaStateServer()
    server.add_transport(RiftKaradaTransport(send, lambda: True))
    await server.set_presence("summon", "test")
    await server.set_presence("summon", "test")
    assert server.get_presence() == "summon"
    assert [f["payload"]["state"] for f in got] == ["summon"], "the same presence is not told twice"
    assert (await server.get_full_state())["presence"] == "summon", "a client that joins late is told too"
    await server.set_presence(None, "test")
    assert got[-1]["payload"]["state"] == "idle" and "presence" not in (await server.get_full_state())

    class Plain(KaradaTransport):  # a transport that knows nothing of presence (the WebUI's) must not break
        async def broadcast_animation(self, payload): ...
        async def broadcast_audio(self, payload): ...
        async def broadcast_face(self, payload): ...
        async def broadcast_model(self, payload): ...
        async def broadcast_expression(self, payload): ...
        async def send_to_session(self, session_id, payload): ...
        async def preload_asset(self, session_id, payload): ...
        def get_connected_sessions(self): return []

    server.add_transport(Plain())
    await server.set_presence("summon", "test")


# -- voice: in RetroXR she speaks as well as writes --------------------------


class FakeVox:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.timelines: list[tuple[Any, ...]] = []
        self.ok = ok

    def _schedule_expression_timeline(self, events: Any, clean: str, path: Any, duration: Any) -> None:
        self.timelines.append((len(events), clean, path, duration))

    async def speak(self, text: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"text": text, **kw})
        if not self.ok:
            return {"status": "skipped", "reason": "vox_disabled"}
        return {"status": "success", "audio_path": "/tmp/vox_1.wav", "audio_duration_s": 1.5,
                "lipsync_data": {"v": [1]}}


async def _say(conn: RetroXRConnector, ws: FakeSocket, text: str) -> VesselActionResult:
    task = asyncio.create_task(conn.act("say", {"text": text}))
    for _ in range(5):
        await asyncio.sleep(0)
    frame = next(f for f in ws.sent if f.get("type") == "action")
    await conn.handle_frame({"type": "action_result", "id": frame["id"], "ok": True})
    res = await task
    if conn._speech_tasks:
        await asyncio.gather(*list(conn._speech_tasks))
    return res


async def test_say_is_always_spoken(monkeypatch: pytest.MonkeyPatch, karada: FakeKarada) -> None:
    from core import core_initializer

    vox = FakeVox()
    monkeypatch.setitem(core_initializer.PLUGIN_REGISTRY, "vox_plugin", vox)
    conn, ws = await _connected()
    res = await _say(conn, ws, "Ciao, sono qui.")
    assert res.ok
    action = next(f for f in ws.sent if f.get("type") == "action")
    assert action["payload"]["audio"] is True, "the audio flag is forced even though Synth did not ask"
    assert action["payload"]["text"] == "Ciao, sono qui."
    assert vox.calls == [{"text": "Ciao, sono qui.", "interface_path": None, "generate_only": True,
                          "allow_fallback": False}], "Vox synthesises only: Karada does the fan-out"
    assert karada.audio == [{"audio_path": "/tmp/vox_1.wav", "lipsync_data": {"v": [1]},
                             "audio_duration_s": 1.5, "text": "Ciao, sono qui."}]


async def test_expression_tags_are_not_shown_and_move_her_face(monkeypatch: pytest.MonkeyPatch, karada: FakeKarada) -> None:
    from core import core_initializer

    vox = FakeVox()
    monkeypatch.setitem(core_initializer.PLUGIN_REGISTRY, "vox_plugin", vox)
    conn, ws = await _connected()
    await _say(conn, ws, "[em_smile:0.6]Che bella stanza!")
    action = next(f for f in ws.sent if f.get("type") == "action")
    assert action["payload"]["text"] == "Che bella stanza!", "the caption has no tags in it"
    assert karada.audio[0]["text"] == "Che bella stanza!"
    assert vox.timelines == [(1, "Che bella stanza!", None, 1.5)], "her face follows the tag, over the clip's length"


async def test_no_voice_engine_leaves_the_line_as_text(monkeypatch: pytest.MonkeyPatch, karada: FakeKarada) -> None:
    from core import core_initializer

    monkeypatch.delitem(core_initializer.PLUGIN_REGISTRY, "vox_plugin", raising=False)
    conn, ws = await _connected()
    assert (await _say(conn, ws, "Solo testo")).ok
    assert karada.audio == []

    vox = FakeVox(ok=False)
    monkeypatch.setitem(core_initializer.PLUGIN_REGISTRY, "vox_plugin", vox)
    conn2, ws2 = await _connected()
    assert (await _say(conn2, ws2, "Vox spento")).ok
    assert len(vox.calls) == 1 and karada.audio == [], "a disabled engine is not an error and plays nothing"


async def test_an_empty_say_is_not_synthesised(monkeypatch: pytest.MonkeyPatch, karada: FakeKarada) -> None:
    from core import core_initializer

    vox = FakeVox()
    monkeypatch.setitem(core_initializer.PLUGIN_REGISTRY, "vox_plugin", vox)
    conn, ws = await _connected()
    await _say(conn, ws, "   ")
    assert vox.calls == []


# -- the idle beat: what is around her ---------------------------------------


async def test_she_is_described_what_is_around_her(karada: FakeKarada) -> None:
    conn, _ = await _connected()
    assert conn.uses_idle_beat is True
    await conn.handle_frame({"type": "state", "state": {"position": {"x": 0, "y": 0, "z": 0}, "holding": "",
        "extra": {"entities": [
            {"name": "player", "type": "player", "label": "the player", "distance": 2.4, "side": "straight ahead"},
            {"name": "Tv@1", "type": "RetroTV", "label": "Panasonic TV", "distance": 1.8,
             "side": "ahead and to the left", "powered": True},
            {"name": "NES@2", "type": "RetroSystem", "label": "NES", "systemid": "nes", "distance": 2.1,
             "side": "to your right", "powered": False},
        ]}}})
    text = conn.describe_surroundings() or ""
    assert "The player is straight ahead, 2.4 m away." in text
    assert "Panasonic TV (switched on), ahead and to the left, 1.8 m" in text
    assert "NES (a nes machine, switched off), to your right, 2.1 m" in text
    assert "Your hands are empty." in text
    # she is not scripted: this is a description, never an instruction
    assert "you must" not in text.lower() and "go to" not in text.lower()


async def test_nothing_to_describe_before_the_rift_is_open() -> None:
    assert RetroXRConnector().describe_surroundings() is None


async def test_alone_she_is_told_so(karada: FakeKarada) -> None:
    conn, _ = await _connected()
    await conn.handle_frame({"type": "state", "state": {"extra": {"entities": []}, "holding": "a cartridge"}})
    text = conn.describe_surroundings() or ""
    assert "You cannot see the player." in text and "There is nothing else near you." in text
    assert "You are holding a cartridge." in text


# -- core.vessel_beat: the pure half -----------------------------------------


def _cfg(values: dict[str, Any]):
    def get(key: str, default: Any) -> Any:
        return values.get(key, default)

    return get


def test_idle_prompt_describes_and_asks_and_allows_doing_nothing() -> None:
    p = vessel_beat.build_idle_prompt("retroxr", "- a TV, 2 m")
    assert "nobody is talking to you" in p and "- a TV, 2 m" in p and "What do you want to do?" in p
    assert "do nothing" in p, "not acting is an explicit option: she is never forced"
    assert "(nothing you can make out)" in vessel_beat.build_idle_prompt("retroxr", "  ")


def test_idle_beat_waits_for_the_interval_and_for_the_player_to_be_quiet() -> None:
    due = vessel_beat.idle_beat_due
    assert due(100, 0, 90, 0, 45) is True, "nobody has ever spoken, interval passed"
    assert due(100, 50, 90, 0, 45) is False, "too soon after the last beat"
    assert due(200, 0, 90, 190, 45) is False, "a player spoke 10 s ago: the chat comes first"
    assert due(300, 0, 90, 190, 45) is True, "110 s of quiet"
    assert due(200, 0, 90, 199, 0) is True, "a quiet window of 0 disables the deferral"


def test_idle_beat_config_is_clamped_and_defaults_on() -> None:
    assert vessel_beat.is_idle_beat_enabled(_cfg({})) is True
    assert vessel_beat.is_idle_beat_enabled(_cfg({"VESSEL_IDLE_BEAT_ENABLED": False})) is False
    assert vessel_beat.resolve_idle_interval(_cfg({})) == 90
    assert vessel_beat.resolve_idle_interval(_cfg({"VESSEL_IDLE_BEAT_INTERVAL_SEC": 1})) == 20
    assert vessel_beat.resolve_idle_interval(_cfg({"VESSEL_IDLE_BEAT_INTERVAL_SEC": 10**6})) == 3600
    assert vessel_beat.resolve_idle_quiet_sec(_cfg({})) == 45
    assert vessel_beat.resolve_idle_quiet_sec(_cfg({"VESSEL_IDLE_BEAT_QUIET_SEC": -5})) == 0


# -- the interface: it asks, once, only for a world that wants it ------------


class _Conn:
    def __init__(self, idle: bool, text: str | None = "- a TV, 2 m") -> None:
        self.is_connected = True
        self.uses_idle_beat = idle
        self._text = text

    def describe_surroundings(self) -> str | None:
        return self._text


@pytest.fixture
def iface(monkeypatch: pytest.MonkeyPatch):
    from core import vessel_session_manager as vsm
    from core.vessel_registry import VESSEL_REGISTRY
    from interface.vessel_interface import VesselInterface

    monkeypatch.setattr(vsm.vessel_session_manager, "has_active_session", lambda: True)
    monkeypatch.setattr("interface.vessel_interface.get_vessel_session_manager", lambda: vsm.vessel_session_manager)
    monkeypatch.setattr(VESSEL_REGISTRY, "_instances", {})
    vi = VesselInterface()
    sent: list[dict[str, Any]] = []

    async def enqueue(**kw: Any) -> None:
        sent.append(kw)

    monkeypatch.setattr(vi, "_enqueue_perception", enqueue)
    return vi, VESSEL_REGISTRY, sent


async def test_idle_beat_asks_what_she_wants_to_do(iface) -> None:
    vi, reg, sent = iface
    reg._instances["retroxr"] = _Conn(idle=True)
    await vi._maybe_run_idle_beat()
    assert len(sent) == 1
    assert sent[0]["event_type"] == "idle_beat" and sent[0]["environment"] == "retroxr"
    assert "- a TV, 2 m" in sent[0]["summary"] and "What do you want to do?" in sent[0]["summary"]
    assert "allowed_actions" not in sent[0], "nothing is restricted: she may do anything she really can"
    await vi._maybe_run_idle_beat()
    assert len(sent) == 1, "and not again until the interval has passed"


async def test_idle_beat_yields_to_a_player_who_just_spoke(iface) -> None:
    vi, reg, sent = iface
    reg._instances["retroxr"] = _Conn(idle=True)
    vi._last_player_activity_at = asyncio.get_event_loop().time()
    await vi._maybe_run_idle_beat()
    assert sent == [], "a player's chat outranks and postpones it"


async def test_idle_beat_is_only_for_worlds_that_asked(iface) -> None:
    vi, reg, sent = iface
    reg._instances["minecraft"] = _Conn(idle=False)
    await vi._maybe_run_idle_beat()
    assert sent == []
    reg._instances.clear()
    reg._instances["retroxr"] = _Conn(idle=True, text=None)
    await vi._maybe_run_idle_beat()
    assert sent == [], "nothing to describe, nothing to ask"


async def test_an_idle_world_has_no_goal_beats(iface, monkeypatch: pytest.MonkeyPatch) -> None:
    vi, reg, sent = iface
    reg._instances["retroxr"] = _Conn(idle=True)
    assert vi._uses_idle_beat("retroxr") is True
    reg._instances["minecraft"] = _Conn(idle=False)
    assert vi._uses_idle_beat("minecraft") is False
    assert vi._uses_idle_beat(None) is False


async def test_the_goal_beats_do_not_run_for_an_idle_world(iface, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with autonomy switched on, a world that is asked what she wants gets no will,
    action, goal or reflection beat: those exist to push a bot toward a goal."""
    import interface.vessel_interface as vi_mod

    vi, reg, sent = iface

    class Conn(_Conn):
        async def get_world_state(self) -> Any:
            return SimpleNamespace(environment="retroxr", health=None, position=None, possible_actions=[],
                                   flags={}, extra={"entities": []})

    def get_value(key: str, default: Any = None, **kw: Any) -> Any:
        return {"VESSEL_AUTONOMY_ENABLED": True, "VESSEL_WILL_QUIET_SEC": 0,
                "VESSEL_REFLECTION_ENABLED": True}.get(key, default)

    monkeypatch.setattr(vi_mod.config_registry, "get_value", get_value)
    reg._instances["retroxr"] = Conn(idle=True)
    for beat in (vi._maybe_run_will_beat, vi._maybe_run_action_beat, vi._maybe_run_goal_beat, vi._maybe_run_reflection):
        await beat()
    assert sent == []
    # the control: the same world without the opt-in DOES get a will beat
    reg._instances.clear()
    reg._instances["minecraft"] = Conn(idle=False)
    await vi._maybe_run_will_beat()
    assert [e["event_type"] for e in sent] == ["will_beat"], "(the control: a goal-driven world still does)"


# -- priorities: the Vessel is above background work, a player's chat above the Vessel ----


@pytest.mark.asyncio
async def test_rift_priorities(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.message_queue as mq
    from tests.test_vessel_realtime import _make_message, _patch_enqueue_hot_path, _semantic_priority

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=True)
    # What the idle beat enqueues: an ordinary vessel perception on the RetroXR world.
    await mq.enqueue(bot=None, message=_make_message("vessel/retroxr/den"), interface_id="vessel",
                     skip_mention_check=True)
    idle = _semantic_priority(put_items)
    # What a player typing to her enqueues.
    await mq.enqueue(bot=None, message=_make_message("vessel/retroxr/den", vessel_player_chat=True),
                     interface_id="vessel", skip_mention_check=True)
    chat = _semantic_priority(put_items)
    assert idle == mq.PRIORITY_AMBIENT and chat == mq.PRIORITY_HIGH
    assert chat > mq.PRIORITY_GENERAL > idle, "her chat first, then ordinary chat, then her own idle turn"
    assert idle > mq.PRIORITY_BACKGROUND and idle > mq.PRIORITY_LOW, "the Vessel outranks G.R.I.L.L.O. and background work"


# -- she is only offered what her body can do --------------------------------


def _plugin_in_retroxr(monkeypatch: pytest.MonkeyPatch, verbs: list[str] | None):
    """A VesselPlugin embodied in a RetroXR whose mod declared ``verbs`` (None: said nothing)."""
    from plugins.rift_vessel.vessel_plugin import VesselPlugin

    monkeypatch.setattr(VesselPlugin, "refresh_config", lambda self: None)
    monkeypatch.setattr(VesselPlugin, "_import_builtin_connectors", staticmethod(lambda: None))
    monkeypatch.setattr("plugins.rift_vessel.vessel_plugin.register_plugin", lambda name, inst: None)
    plugin = VesselPlugin()
    conn = RetroXRConnector()
    hello: dict[str, Any] = {"room": "den"}
    if verbs is not None:
        hello["verbs"] = verbs
    conn.attach_socket(FakeSocket(), hello)
    conn._connected = True
    monkeypatch.setattr("plugins.rift_vessel.vessel_plugin.VESSEL_REGISTRY.load_connector", lambda name: conn)
    monkeypatch.setattr(VesselPlugin, "_connected_world", lambda self: "retroxr")
    return plugin, conn


def test_only_what_the_mod_can_do_is_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin, _ = _plugin_in_retroxr(monkeypatch, ["say", "move", "look", "status", "emote", "look_at", "inspect", "follow", "unfollow"])
    offered = {n.removeprefix("vessel_retroxr_") for n in plugin.get_supported_actions() if n.startswith("vessel_retroxr_")}
    assert offered == {"say", "move", "look", "status", "emote", "look_at", "inspect", "follow", "unfollow"}
    # what a RetroXR body cannot do is not on offer, and neither is the Minecraft way of looking around
    for gone in ("attack", "respawn", "use", "observe", "pick_up", "put_down", "hand_over", "sit", "touch"):
        assert gone not in offered, gone
    assert "vessel_disconnect" in plugin.get_supported_actions()


def test_a_mod_that_says_nothing_is_offered_very_little(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old mod declares no verbs: better to offer five that work than thirteen that mostly do not."""
    plugin, _ = _plugin_in_retroxr(monkeypatch, None)
    offered = {n.removeprefix("vessel_retroxr_") for n in plugin.get_supported_actions() if n.startswith("vessel_retroxr_")}
    assert offered == {"say", "move", "look", "status", "emote"}


def test_other_worlds_still_offer_every_core_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    """core_verbs is opt-in: a world that does not say gets the historic full set."""
    from plugins.rift_vessel.vessel_base import VesselConnectorBase
    from plugins.rift_vessel.vessel_plugin import VesselPlugin

    assert VesselConnectorBase.core_verbs is None
    assert VesselPlugin._core_verbs_for("") is None and VesselPlugin._core_verbs_for("vessel") is None


async def test_a_known_verb_the_mod_did_not_declare_is_refused_here(karada: FakeKarada) -> None:
    conn, ws = await _connected({"room": "den", "verbs": ["say"]})
    res = await conn.act("inspect", {"target": "tv"})
    assert res.ok is False and res.detail == "not_available: inspect"
    assert ws.sent == [], "it never reaches the room to fail there"
