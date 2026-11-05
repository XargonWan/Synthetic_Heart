"""RetroXR: her body follows Karada (states, voice), she speaks as well as writes, and when
nobody is talking to her she is TOLD what is around her and asked what she wants to do.

No DB, no LLM, no RetroXR: the mod is a fake socket, Karada and Vox are stubs.
"""

from __future__ import annotations

import asyncio
import time
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
    def __init__(self, ok: bool = True, duration: float = 1.5, delays: list[float] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.timelines: list[tuple[Any, ...]] = []
        self.ok = ok
        self.duration = duration
        self.delays = list(delays or [])  # how long the n-th synthesis takes

    def _schedule_expression_timeline(self, events: Any, clean: str, path: Any, duration: Any) -> None:
        self.timelines.append((len(events), clean, path, duration))

    async def speak(self, text: str, **kw: Any) -> dict[str, Any]:
        n = len(self.calls)
        self.calls.append({"text": text, **kw})
        if n < len(self.delays):
            await asyncio.sleep(self.delays[n])
        if not self.ok:
            return {"status": "skipped", "reason": "vox_disabled"}
        return {"status": "success", "audio_path": f"/tmp/vox_{n + 1}.wav", "audio_duration_s": self.duration,
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
    assert "stay where you are" in p, "not acting is still a choice: she is never forced"
    assert "nobody asked for it" in p, "what she does is hers, not a task for a requester"
    assert "must" not in p.lower(), "a description and a question, never an order"
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
    assert vessel_beat.resolve_idle_interval(_cfg({})) == 40
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


async def test_idle_beat_waits_until_the_engine_has_rested(iface, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn nobody asked for must not run while a person could be waiting: the engine must have been free for a
    few seconds (the queue's own clock), not merely 40 s since the last beat was ENQUEUED."""
    from core import message_queue

    vi, reg, sent = iface
    reg._instances["retroxr"] = _Conn(idle=True)
    monkeypatch.setattr(message_queue, "consumer_idle_seconds", lambda: 0.0)  # a turn in flight / queued
    await vi._maybe_run_idle_beat()
    assert sent == [], "a beat was started on a busy engine"
    monkeypatch.setattr(message_queue, "consumer_idle_seconds", lambda: 5.0)  # free, but only for 5 s of 12
    await vi._maybe_run_idle_beat()
    assert sent == [], "a beat was started before the engine had rested"
    monkeypatch.setattr(message_queue, "consumer_idle_seconds", lambda: 15.0)
    await vi._maybe_run_idle_beat()
    assert len(sent) == 1


def test_the_queue_says_how_long_it_has_been_free(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import message_queue as mq

    monkeypatch.setattr(mq, "_queue", None)
    monkeypatch.setattr(mq, "_in_flight_label", None)
    monkeypatch.setattr(mq, "_last_item_finished_at", None)
    assert mq.consumer_idle_seconds() == float("inf"), "never busy: nothing to wait for"
    monkeypatch.setattr(mq, "_in_flight_label", "chat x")
    assert mq.consumer_idle_seconds() == 0.0, "a turn in flight is busy"
    monkeypatch.setattr(mq, "_in_flight_label", None)
    mq._clear_item_in_flight()
    assert 0.0 <= mq.consumer_idle_seconds() < 5.0, "just finished: just rested"


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
    assert offered == {"say", "move", "look", "status", "emote", "look_at", "inspect", "follow", "unfollow",
                       "set_goal", "update_goal", "explore"}, "her own aims and her walks are always hers"
    # what a RetroXR body cannot do is not on offer, and neither is the Minecraft way of looking around
    for gone in ("attack", "respawn", "use", "observe", "pick_up", "put_down", "hand_over", "sit", "touch"):
        assert gone not in offered, gone
    assert "vessel_disconnect" in plugin.get_supported_actions()


def test_a_mod_that_says_nothing_is_offered_very_little(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old mod declares no verbs: better to offer five that work than thirteen that mostly do not."""
    plugin, _ = _plugin_in_retroxr(monkeypatch, None)
    offered = {n.removeprefix("vessel_retroxr_") for n in plugin.get_supported_actions() if n.startswith("vessel_retroxr_")}
    assert offered == {"say", "move", "look", "status", "emote", "set_goal", "update_goal", "explore"}


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


async def test_a_long_reply_is_voiced_sentence_by_sentence_and_its_text_waits_for_the_first(
    monkeypatch: pytest.MonkeyPatch, karada: FakeKarada
) -> None:
    """The voice used to come after the text because the WHOLE reply was synthesized first. Now the first sentence is
    on its way while the next is still being made, and the text goes out with it, not before."""
    from core import core_initializer

    # sentence 1 takes 0.1 s, sentence 2 takes 0.6 s: the text must not wait for the second.
    vox = FakeVox(duration=0.05, delays=[0.1, 0.6])
    monkeypatch.setitem(core_initializer.PLUGIN_REGISTRY, "vox_plugin", vox)
    conn, ws = await _connected()
    task = asyncio.create_task(conn.act("say", {"text": "Ciao Jay, eccomi qui. Ho guardato la stanza con calma."}))
    for _ in range(40):
        if any(f.get("type") == "action" for f in ws.sent):
            break
        await asyncio.sleep(0.01)
    action = next(f for f in ws.sent if f.get("type") == "action")
    assert len(karada.audio) == 1, "the first clip is out before the text"
    assert [c["text"] for c in vox.calls][0] == "Ciao Jay, eccomi qui."
    assert len(vox.calls) == 2, "and the second sentence is already being made behind it"
    await conn.handle_frame({"type": "action_result", "id": action["id"], "ok": True})
    await task
    await asyncio.gather(*list(conn._speech_tasks))
    assert [a["text"] for a in karada.audio] == ["Ciao Jay, eccomi qui.", "Ho guardato la stanza con calma."]
    assert action["payload"]["text"].startswith("Ciao Jay") and action["payload"]["text"].endswith("calma.")


async def test_each_sentence_brings_its_own_expression_tags(monkeypatch: pytest.MonkeyPatch, karada: FakeKarada) -> None:
    from core import core_initializer

    vox = FakeVox(duration=0.05)
    monkeypatch.setitem(core_initializer.PLUGIN_REGISTRY, "vox_plugin", vox)
    conn, ws = await _connected()
    await _say(conn, ws, "[em_smile:0.6]Che bella stanza, davvero. [em_sad:0.4]Peccato per il disordine qui.")
    assert [t[1] for t in vox.timelines] == ["Che bella stanza, davvero.", "Peccato per il disordine qui."], vox.timelines
    assert all(t[0] == 1 for t in vox.timelines), "one tag each, timed over its own clip"


async def test_a_voice_that_is_too_slow_does_not_hold_the_text_for_ever(monkeypatch: pytest.MonkeyPatch, karada: FakeKarada) -> None:
    from core import core_initializer
    from plugins.rift_vessel.retroxr import retroxr as mod

    monkeypatch.setattr(mod, "_SAY_HOLD_SEC", 0.2)
    vox = FakeVox(duration=0.05, delays=[1.0])
    monkeypatch.setitem(core_initializer.PLUGIN_REGISTRY, "vox_plugin", vox)
    conn, ws = await _connected()
    task = asyncio.create_task(conn.act("say", {"text": "Una frase lenta da sintetizzare."}))
    for _ in range(40):
        if any(f.get("type") == "action" for f in ws.sent):
            break
        await asyncio.sleep(0.01)
    assert any(f.get("type") == "action" for f in ws.sent) and karada.audio == [], "the text went alone after the hold"
    action = next(f for f in ws.sent if f.get("type") == "action")
    await conn.handle_frame({"type": "action_result", "id": action["id"], "ok": True})
    await task
    await asyncio.gather(*list(conn._speech_tasks))
    assert len(karada.audio) == 1, "and the voice still follows"


async def test_an_early_first_prompt_keeps_the_introduction_open_until_they_are_out(karada: FakeKarada) -> None:
    """With `hello.early_summon` the `summoned` event goes at the welcome; the presence must last until the `out` frame."""
    from core.animation_handler import get_karada_state_server

    conn, ws = await _connected({"room": "den", "early_summon": True})
    server = get_karada_state_server()
    await server.set_presence("summon", "test")
    await conn._dispatch_event({"event_type": "summoned", "summary": "You are being summoned", "salience": 1.0})
    await asyncio.sleep(0)
    assert server.get_presence() == "summon", "the first prompt alone does not end the introduction"
    await conn.handle_frame({"type": "out"})
    assert server.get_presence() is None, "stepping out does"


async def test_an_older_mod_still_ends_the_introduction_with_its_summoned_event(karada: FakeKarada) -> None:
    from core.animation_handler import get_karada_state_server

    conn, ws = await _connected({"room": "den"})
    server = get_karada_state_server()
    await server.set_presence("summon", "test")
    await conn._dispatch_event({"event_type": "summoned", "summary": "You have just been summoned", "salience": 1.0})
    assert server.get_presence() is None


# -- the beat is paced, remembers, and says why it did not start -------------


def test_the_interval_is_quick_on_arrival_settles_stretches_and_springs_back() -> None:
    iv = vessel_beat.adaptive_idle_interval
    base, arrive, window, top = 40, 20, 180, 180
    assert iv(base, arrive, window, top, since_ready=30, idle_streak=0) == 20, "just arrived: quick"
    assert iv(base, arrive, window, top, since_ready=600, idle_streak=0) == 40, "settled"
    assert iv(base, arrive, window, top, since_ready=600, idle_streak=3) == 40, "three quiet beats are free"
    assert iv(base, arrive, window, top, since_ready=600, idle_streak=4) == 60, "then it stretches"
    assert iv(base, arrive, window, top, since_ready=600, idle_streak=5) == 90
    assert iv(base, arrive, window, top, since_ready=600, idle_streak=40) == top, "never past the cap"
    assert iv(base, arrive, window, top, since_ready=600, idle_streak=6, novelty=True) == 20, (
        "a change around her brings the next ask forward, however quiet she has been"
    )
    assert iv(15, 20, window, top, since_ready=0, idle_streak=0) == 15, "the arrival pace is never slower than base"


def test_the_idle_config_has_sane_clamps() -> None:
    assert vessel_beat.resolve_idle_arrival_interval(_cfg({})) == 20
    assert vessel_beat.resolve_idle_arrival_interval(_cfg({"VESSEL_IDLE_BEAT_ARRIVAL_INTERVAL_SEC": 1})) == 10
    assert vessel_beat.resolve_idle_arrival_window(_cfg({})) == 180
    assert vessel_beat.resolve_idle_max_interval(_cfg({})) == 180
    assert vessel_beat.resolve_idle_engine_rest_sec(_cfg({})) == 12
    assert vessel_beat.resolve_idle_engine_rest_sec(_cfg({"VESSEL_IDLE_BEAT_ENGINE_REST_SEC": "x"})) == 12


def test_the_idle_prompt_carries_facts_not_orders() -> None:
    p = vessel_beat.build_idle_prompt(
        "retroxr", "- a TV, 2 m", recent=[(240, "watch Game Boy Color"), (20, 'said "hi"')], still_seconds=420,
        changes=["a NES was put down near you"],
    )
    assert "Since you last looked, this changed:\n- a NES was put down near you" in p
    assert "- 4 min ago: watch Game Boy Color" in p and "- 20 s ago: said \"hi\"" in p
    assert "standing still for 7 minutes" in p
    assert "You have not done anything yet." not in p
    fresh = vessel_beat.build_idle_prompt("retroxr", "- a TV, 2 m", recent=[], still_seconds=5)
    assert "You have not done anything yet." in fresh
    assert "standing still" not in fresh, "five seconds is not worth saying"
    bare = vessel_beat.build_idle_prompt("retroxr", "- a TV, 2 m")
    assert "What you did lately" not in bare and "Since you last looked" not in bare


async def test_nothing_is_described_until_her_eyes_have_reported() -> None:
    conn, _ = await _connected()
    assert conn.describe_surroundings() is None, "no state yet: 'nothing near you' would be a lie (it cost the first beat)"
    assert conn.idle_ready_since() is None


async def test_she_is_ready_to_be_asked_once_she_is_out(karada: FakeKarada) -> None:
    conn, _ = await _connected()
    await conn.handle_frame({"type": "state", "state": {"extra": {"entities": []}}})
    assert conn.idle_ready_since() is None, "she is still in the capsule"
    await conn.handle_frame({"type": "out"})
    ready = conn.idle_ready_since()
    assert ready is not None and abs(ready - time.monotonic()) < 5


async def test_a_session_with_no_out_frame_is_ready_a_little_after_it_attached(karada: FakeKarada) -> None:
    """A resumed session / an older mod sends no ``out``: she must not wait for ever."""
    conn, _ = await _connected()
    await conn.handle_frame({"type": "state", "state": {"extra": {"entities": []}}})
    assert conn.idle_ready_since() is None
    conn._attached_at = time.monotonic() - 100
    assert conn.idle_ready_since() is not None


async def test_what_she_did_is_kept_for_the_prompt(karada: FakeKarada) -> None:
    conn, _ = await _connected()
    conn._journal_action("inspect", {"target": "Game Boy Color"}, VesselActionResult(ok=True))
    conn._journal_action("move", {"target": "TV"}, VesselActionResult(ok=False, detail="no_way_round"))
    conn._journal_action("status", {}, VesselActionResult(ok=True))
    conn._journal_action("say", {"text": "hello"}, VesselActionResult(ok=True))
    await conn._dispatch_event({"event_type": "search_done", "summary": "You found the NES behind the pod."})
    await conn._dispatch_event({"event_type": "sighting", "summary": "a TV was switched on"})
    texts = [t for _, t in conn.idle_journal()]
    assert texts == [
        "inspect Game Boy Color",
        "move TV (did not work: no_way_round)",
        'said "hello"',
        "You found the NES behind the pod.",
    ], "status is a read, a sighting is something that happened TO her, a *_done is hers"


async def test_the_journal_is_bounded_and_ends_with_the_session(karada: FakeKarada) -> None:
    conn, _ = await _connected()
    for n in range(40):
        conn._note(f"thing {n}")
    assert len(conn.idle_journal()) == 12 and conn.idle_journal()[-1][1] == "thing 39"
    conn._note("x" * 500)
    assert len(conn.idle_journal()[-1][1]) == 110
    await conn.disconnect()
    assert conn.idle_journal() == []


class _Ready(_Conn):
    """A connector with a readiness clock and a journal she can add to."""

    def __init__(self, ready: float | None, text: str | None = "- a TV, 2 m") -> None:
        super().__init__(idle=True, text=text)
        self.ready = ready
        self.journal: list[tuple[float, str]] = []

    def idle_ready_since(self) -> float | None:
        return self.ready

    def idle_journal(self) -> list[tuple[float, str]]:
        return list(self.journal)


@pytest.fixture
def rested(monkeypatch: pytest.MonkeyPatch):
    """The engine has been free for a long time (and can be told otherwise)."""
    from core import message_queue

    state = {"free": 100.0}
    monkeypatch.setattr(message_queue, "consumer_idle_seconds", lambda: state["free"])
    return state


async def test_no_beat_before_she_is_out_and_the_first_one_waits_the_arrival_pace(iface, rested) -> None:
    vi, reg, sent = iface
    conn = _Ready(ready=None)
    reg._instances["retroxr"] = conn
    await vi._maybe_run_idle_beat()
    assert sent == [], "still in the capsule: nothing to ask"
    now = asyncio.get_event_loop().time()
    conn.ready = now - 5
    await vi._maybe_run_idle_beat()
    assert sent == [], "5 s out of 20: too soon (it used to ask in the same second as the connect)"
    conn.ready = now - 25
    await vi._maybe_run_idle_beat()
    assert len(sent) == 1 and "- a TV, 2 m" in sent[0]["summary"]


async def test_a_beat_that_ends_in_nothing_stretches_the_next_and_an_action_resets_it(iface, rested) -> None:
    vi, reg, sent = iface
    now = asyncio.get_event_loop().time()
    conn = _Ready(ready=now - 10_000)  # long settled: the interval is 40 s
    reg._instances["retroxr"] = conn
    streaks: list[int] = []
    for n in range(6):
        vi._last_idle_beat_at = now - 10_000 + n  # long ago: due now
        await vi._maybe_run_idle_beat()
        assert len(sent) == n + 1
        streaks.append(vi._idle_streak)
        # its turn ends (the engine is free), and she did nothing
        await vi._maybe_run_idle_beat()
    assert streaks == [0, 1, 2, 3, 4, 5], streaks
    vi._idle_streak = 5
    vi._last_idle_beat_at = now - 70  # 70 s ago: past 40, short of the 90 that streak 5 asks
    before = len(sent)
    await vi._maybe_run_idle_beat()
    assert len(sent) == before, "five quiet beats in a row: it asks less often"
    conn.journal.append((asyncio.get_event_loop().time(), "inspect TV"))
    vi._idle_pending = True
    vi._last_idle_beat_at = now - 70
    conn.journal[-1] = (now - 60, "inspect TV")
    await vi._maybe_run_idle_beat()
    assert vi._idle_streak == 0, "she did something after the last ask: the pace is back"
    assert len(sent) == before + 1


async def test_the_prompt_says_what_she_did_and_how_long_she_has_been_still(iface, rested) -> None:
    vi, reg, sent = iface
    now = asyncio.get_event_loop().time()
    conn = _Ready(ready=now - 10_000)
    conn.journal = [(now - 500, "watch Game Boy Color")]
    reg._instances["retroxr"] = conn
    await vi._maybe_run_idle_beat()
    assert "- 8 min ago: watch Game Boy Color" in sent[0]["summary"]
    assert "standing still for 8 minutes" in sent[0]["summary"]


async def test_a_change_around_her_brings_the_ask_forward_and_is_told(iface, rested, monkeypatch: pytest.MonkeyPatch) -> None:
    vi, reg, sent = iface
    now = asyncio.get_event_loop().time()
    conn = _Ready(ready=now - 10_000)
    reg._instances["retroxr"] = conn
    vi._idle_seen_ready = conn.ready  # the arrival is already behind us
    vi._idle_streak = 6  # a long run of nothing: the interval is stretched
    vi._last_idle_beat_at = now - 30
    await vi._maybe_run_idle_beat()
    assert sent == [], "30 s after the last ask with a stretched interval: nothing yet"
    # the REAL event path, with the database stubbed out: the hook must sit before the salience filter, which
    # drops nearly every sighting (rate limit), or a change would never count
    class _Manager:
        async def start_session(self, *a: Any, **k: Any) -> str:
            return "s1"

        async def record_experience(self, *a: Any, **k: Any) -> None:
            pass

    async def no_log(*a: Any, **k: Any) -> None:
        pass

    monkeypatch.setattr("interface.vessel_interface.get_vessel_session_manager", lambda: _Manager())
    monkeypatch.setattr(vi, "_log_activity", no_log)
    monkeypatch.setattr(vi, "_is_salient", lambda *a, **k: False)
    await vi.on_world_event("retroxr", "sighting", "a NES was put down near you")
    assert vi._idle_novelty == ["a NES was put down near you"], "counted although it was not salient"
    monkeypatch.setattr("interface.vessel_interface.get_vessel_session_manager", lambda: SimpleNamespace(
        has_active_session=lambda: True))
    await vi._maybe_run_idle_beat()
    assert len(sent) == 1 and "- a NES was put down near you" in sent[0]["summary"]
    await vi._maybe_run_idle_beat()
    assert len(sent) == 1
    assert vi._idle_novelty == [], "told once"


async def test_a_new_arrival_starts_the_clock_and_the_streak_again(iface, rested) -> None:
    vi, reg, sent = iface
    now = asyncio.get_event_loop().time()
    conn = _Ready(ready=now - 10_000)
    reg._instances["retroxr"] = conn
    vi._idle_streak = 9
    await vi._maybe_run_idle_beat()
    conn.ready = now - 1  # a second session in the same process
    await vi._maybe_run_idle_beat()
    assert vi._idle_streak == 0 and vi._idle_pending is False
    assert len(sent) == 1, "and the new arrival is not asked in its first second"


async def test_a_player_who_cut_the_beat_short_does_not_count_against_her(iface, rested) -> None:
    vi, reg, sent = iface
    now = asyncio.get_event_loop().time()
    conn = _Ready(ready=now - 10_000)
    reg._instances["retroxr"] = conn
    await vi._maybe_run_idle_beat()
    assert len(sent) == 1
    vi._last_player_activity_at = asyncio.get_event_loop().time() + 1
    await vi._maybe_run_idle_beat()
    assert vi._idle_streak == 0 and vi._idle_pending is False


async def test_every_reason_for_holding_a_beat_is_logged(iface, rested, caplog: pytest.LogCaptureFixture) -> None:
    vi, reg, sent = iface
    logged: list[str] = []
    import interface.vessel_interface as vi_mod

    orig = vi_mod.log_info
    vi_mod.log_info = lambda m, *a, **k: logged.append(str(m))
    try:
        reg._instances["retroxr"] = _Ready(ready=None)
        await vi._maybe_run_idle_beat()
        now = asyncio.get_event_loop().time()
        reg._instances["retroxr"] = _Ready(ready=now - 10_000)
        rested["free"] = 2.0
        await vi._maybe_run_idle_beat()
    finally:
        vi_mod.log_info = orig
    assert any("Idle beat held (not_ready)" in m for m in logged), logged
    assert any("Idle beat held (engine_busy)" in m and "2s of the 12s" in m for m in logged), logged


# -- her own aims: kept by Synth, never sent to the mod ------------------------


class _FakeGoals:
    """Stands in for plugins.goals.goals (a database)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.active: dict[str, Any] | None = None

    async def set_goal(self, description: str, *a: Any, **kw: Any) -> dict[str, Any]:
        self.calls.append(("set", (description, *a), kw))
        self.active = {"description": description, "note": kw.get("note")}
        return {"status": "ok", "message": "goal set"}

    async def update_active_goal(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("update", (), kw))
        return {"status": "ok", "message": "goal updated"}

    async def get_active_goal(self, **kw: Any) -> dict[str, Any] | None:
        self.calls.append(("get", (), kw))
        return self.active


@pytest.fixture
def goals(monkeypatch: pytest.MonkeyPatch) -> _FakeGoals:
    import importlib

    store = importlib.import_module("plugins.goals.goals")
    fake = _FakeGoals()
    monkeypatch.setattr(store, "set_goal", fake.set_goal)
    monkeypatch.setattr(store, "update_active_goal", fake.update_active_goal)
    monkeypatch.setattr(store, "get_active_goal", fake.get_active_goal)
    return fake


async def test_her_aims_are_kept_in_the_goals_store_and_never_sent_to_the_mod(karada: FakeKarada, goals: _FakeGoals) -> None:
    conn, ws = await _connected()
    res = await conn.act("set_goal", {"description": "find out what is in this room", "note": "start with the shelf"})
    assert res.ok is True
    kind, args, kw = goals.calls[0]
    assert args[0] == "find out what is in this room"
    assert (kw["scope"], kw["game"], kw["world"]) == ("vessel", "retroxr", "none"), "one house, one set of aims"
    assert kw["note"] == "start with the shelf"
    res = await conn.act("update_goal", {"note": "got through the shelf", "status": "done"})
    assert res.ok is True and goals.calls[1][2]["status"] == "done"
    assert ws.sent == [], "what she wants is not something her body does: nothing reaches the room"
    assert (await conn.idle_intention() or {}).get("description") == "find out what is in this room"


async def test_an_aim_needs_words_and_a_broken_store_is_an_answer_not_a_crash(karada: FakeKarada, goals: _FakeGoals, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, ws = await _connected()
    res = await conn.act("set_goal", {"note": "x"})
    assert res.ok is False and "description" in (res.detail or "") and goals.calls == []
    res = await conn.act("set_goal", {"goal": "an alias a weaker model might use"})
    assert res.ok is True and goals.calls[0][1][0] == "an alias a weaker model might use"

    import importlib

    store = importlib.import_module("plugins.goals.goals")

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("no database")

    monkeypatch.setattr(store, "set_goal", boom)
    monkeypatch.setattr(store, "get_active_goal", boom)
    res = await conn.act("set_goal", {"description": "keep going"})
    assert res.ok is False and "no database" in (res.detail or "")
    assert await conn.idle_intention() is None, "no store, no aim: the beat still asks"
    assert ws.sent == []


async def test_a_closed_rift_still_keeps_no_aims_it_cannot_act_on(goals: _FakeGoals) -> None:
    res = await RetroXRConnector().act("set_goal", {"description": "x"})
    assert res.ok is True or res.detail == "rift_closed"  # kept either way; never a crash


def test_the_idle_prompt_offers_her_aim_back_as_hers() -> None:
    with_aim = vessel_beat.build_idle_prompt(
        "retroxr", "- a TV, 2 m", intention={"description": "watch the whole of Sonic", "note": "got to the second zone"}
    )
    assert "your own aim: watch the whole of Sonic" in with_aim
    assert "Your last note on it: got to the second zone" in with_aim
    assert "vessel_retroxr_update_goal" in with_aim and "vessel_retroxr_set_goal" in with_aim
    assert "must" not in with_aim.lower()
    without = vessel_beat.build_idle_prompt("retroxr", "- a TV, 2 m")
    assert "You have no aim of your own at the moment." in without
    assert "Nobody assigns it." in without and "must" not in without.lower()
    assert "your own aim:" not in without


async def test_the_beat_hands_her_current_aim_to_the_prompt(iface, rested) -> None:
    vi, reg, sent = iface

    class Conn(_Ready):
        async def idle_intention(self) -> dict[str, Any]:
            return {"description": "tidy the shelf", "note": None}

    now = asyncio.get_event_loop().time()
    reg._instances["retroxr"] = Conn(ready=now - 10_000)
    await vi._maybe_run_idle_beat()
    assert "your own aim: tidy the shelf" in sent[0]["summary"]


async def test_a_store_that_fails_does_not_stop_the_beat(iface, rested) -> None:
    vi, reg, sent = iface

    class Conn(_Ready):
        async def idle_intention(self) -> dict[str, Any]:
            raise RuntimeError("database gone")

    now = asyncio.get_event_loop().time()
    reg._instances["retroxr"] = Conn(ready=now - 10_000)
    await vi._maybe_run_idle_beat()
    assert len(sent) == 1 and "You have no aim of your own" in sent[0]["summary"]


# -- explore: the core decides the route, the body answers one question at a time ----------


class FakeMod(FakeSocket):
    """A mod that answers every action frame, the way the real one answers a survey, a walk and a look."""

    def __init__(self, conn: RetroXRConnector, script) -> None:
        super().__init__()
        self.conn = conn
        self.script = script
        self.walks = 0

    async def send_json(self, payload: dict[str, Any]) -> None:
        await super().send_json(payload)
        if payload.get("type") != "action":
            return
        ok, detail, data = self.script(self, payload["action"], payload.get("payload") or {})
        asyncio.get_event_loop().call_soon(
            lambda: asyncio.ensure_future(
                self.conn.handle_frame({"type": "action_result", "id": payload["id"], "ok": ok, "detail": detail, "data": data})
            )
        )


def _room_script(max_walks: int = 6):
    """A room: two reachable stops, then nothing new. A walk moves the body to where it was told."""
    state = {"at": {"x": 0.0, "y": 0.0, "z": 0.0}}

    def script(mod: FakeMod, verb: str, payload: dict[str, Any]):
        if verb == "survey":
            if mod.walks >= max_walks:
                return True, "", {"position": state["at"], "candidates": [], "pois": [], "hazards": []}
            return True, "", {
                "position": state["at"], "heading": 0.0,
                "candidates": [{"pos": {"x": 4.0 + mod.walks * 3, "y": 0.0, "z": 2.0}, "cost": 4.5, "seen": False}],
                # in sight until she has gone up to it (the core drops a thing it has studied)
                "pois": [{"name": "Game Boy Color", "pos": {"x": 6.0, "y": 0.0, "z": -3.0}, "cost": 7.0, "area": "den"}],
                "hazards": [{"pos": {"x": -2.0, "y": 0.0, "z": 0.0}, "radius": 0.9, "kind": "obstacle"}],
            }
        if verb == "move":
            mod.walks += 1
            state["at"] = {"x": payload["x"], "y": payload["y"], "z": payload["z"]}
            return True, "arrived", {"position": state["at"]}
        if verb == "look_around":
            return True, "", {"saw": [{"name": f"thing {mod.walks}", "area": "den"}]}
        return True, "ok", {}

    return script


async def _explorer(script, verbs: list[str], on_event) -> tuple[RetroXRConnector, FakeMod]:
    conn = RetroXRConnector()
    mod = FakeMod(conn, script)
    conn.attach_socket(mod, {"room": "den", "verbs": verbs})
    assert await conn.connect({}, on_event) is True
    await conn.handle_frame({"type": "state", "state": {"position": {"x": 0.0, "y": 0.0, "z": 0.0}, "extra": {"entities": []}}})
    return conn, mod


async def _finish(conn: RetroXRConnector) -> None:
    task = conn._explore_state()["task"]
    await asyncio.wait_for(task, 10)


async def test_explore_is_hers_the_mod_is_asked_one_thing_at_a_time(karada: FakeKarada) -> None:
    events: list[Any] = []
    conn, mod = await _explorer(_room_script(), ["say", "move", "stop", "inspect", "survey", "look_around"], events.append)
    conn._explore_state()["policy"].rng.seed(4)   # a fixed walk: which place comes first is a draw
    conn.set_explore_budget(60)
    res = await conn.act("explore", {})
    assert res.ok is True and "going for a walk of about 60 seconds" in (res.detail or "")
    assert conn.idle_busy() is True
    await _finish(conn)
    assert conn.idle_busy() is False
    verbs = [f["action"] for f in mod.sent if f.get("type") == "action"]
    assert verbs[0] == "survey" and "look_around" in verbs[:3], verbs
    moves = [f["payload"] for f in mod.sent if f.get("action") == "move"]
    assert moves and all(m["free"] is True for m in moves), "no leash: the walk asks the mod to leave it off"
    surveys = [f["payload"] for f in mod.sent if f.get("action") == "survey"]
    assert surveys[0]["viewpoints"] == [] and len(surveys[-1]["viewpoints"]) >= 2, "she tells the body where she has looked from"
    assert len(events) == 1 and events[0].event_type == "explore_done"
    d = events[0].data
    assert d["stopped_because"] == "nothing_left" and d["legs"] >= 2
    assert {x["name"] for x in d["discovered"]} >= {"Game Boy Color"}, d
    assert any(x["area"] == "den" for x in d["discovered"])
    assert any("explore" in t for _, t in conn.idle_journal()), "and it counts as something she did"


async def test_the_model_cannot_ask_for_the_internal_verbs_and_is_never_offered_them(karada: FakeKarada) -> None:
    conn, mod = await _explorer(_room_script(), ["say", "survey", "look_around"], lambda e: None)
    assert "survey" not in conn.get_world_actions() and "look_around" not in conn.get_world_actions()
    for verb in ("survey", "look_around"):
        res = await conn.act(verb, {})
        assert res.ok is False and res.detail == f"unknown_verb: {verb}"
    assert mod.sent == []


async def test_without_a_survey_from_the_mod_the_walk_is_blind_but_still_a_walk(karada: FakeKarada) -> None:
    events: list[Any] = []

    def script(mod, verb, payload):
        if verb == "move":
            mod.walks += 1
            return False, "no_way_round: nothing leads from here toward it", {"position": {"x": 0.0, "y": 0.0, "z": 0.0}}
        return True, "ok", {}

    conn, mod = await _explorer(script, ["say", "move"], events.append)
    conn.set_explore_budget(5)
    await conn.act("explore", {})
    await _finish(conn)
    verbs = {f["action"] for f in mod.sent if f.get("type") == "action"}
    assert "survey" not in verbs and "look_around" not in verbs, verbs
    assert events[0].data["stopped_because"] == "blocked" and mod.walks == 3, "three failed walks and she gives up"


async def test_a_person_speaking_stops_the_walk_and_does_not_make_a_second_prompt(karada: FakeKarada) -> None:
    events: list[Any] = []
    gate = asyncio.Event()

    def script(mod, verb, payload):
        return _room_script(max_walks=99)(mod, verb, payload)

    conn, mod = await _explorer(script, ["say", "move", "stop", "survey", "look_around"], events.append)
    conn.set_explore_budget(120)
    await conn.act("explore", {})
    await asyncio.sleep(0.01)
    await conn._dispatch_event({"event_type": "chat", "actor": "player", "summary": "player: ehi", "data": {}})
    await _finish(conn)
    assert conn.idle_busy() is False
    assert [e.event_type for e in events] == ["chat"], "the chat is the next prompt; the walk's end tells the model nothing"
    assert any(f.get("action") == "stop" for f in mod.sent), "the body was told to stop walking now"
    assert any("interrupted" in t for _, t in conn.idle_journal())


async def test_a_dropped_rift_stops_the_walk_and_a_closed_one_will_not_start_it(karada: FakeKarada) -> None:
    events: list[Any] = []
    conn, mod = await _explorer(_room_script(max_walks=99), ["move", "survey", "look_around"], events.append)
    conn.set_explore_budget(120)
    await conn.act("explore", {})
    await asyncio.sleep(0.01)
    conn.pause_socket()
    await _finish(conn)
    assert conn.idle_busy() is False
    assert events == [], "a rift that dropped is not a walk that ended: nobody is there to be told, and it must not be an error"
    res = await conn.act("explore", {})
    assert res.ok is False and res.detail == "rift_closed"


# -- the interface: the walk is sized to the next prompt and holds the beat ----------------


class _Walker(_Ready):
    def __init__(self, ready: float | None) -> None:
        super().__init__(ready)
        self.explores = True
        self.busy = False
        self.budget: float | None = None

    def idle_busy(self) -> bool:
        return self.busy

    def set_explore_budget(self, seconds: float) -> None:
        self.budget = seconds


async def test_a_walk_is_sized_to_the_next_prompt(iface, rested) -> None:
    vi, reg, sent = iface
    now = asyncio.get_event_loop().time()
    conn = _Walker(ready=now - 10_000)
    reg._instances["retroxr"] = conn
    await vi._maybe_run_idle_beat()
    assert len(sent) == 1
    # interval 40 s (settled) + a turn that has not been measured yet (the 25 s guess)
    assert conn.budget == pytest.approx(65.0, abs=0.5), conn.budget


async def test_how_long_a_turn_takes_is_measured_and_the_next_walk_follows_it(iface, rested) -> None:
    vi, reg, sent = iface
    now = asyncio.get_event_loop().time()
    conn = _Walker(ready=now - 10_000)
    reg._instances["retroxr"] = conn
    await vi._maybe_run_idle_beat()
    # the turn took 12 s: the engine became free 12 s after the ask, and has been free for 15 s of the 27 since
    vi._last_idle_beat_at = now - 27
    rested["free"] = 15.0
    await vi._maybe_run_idle_beat()
    assert vi._turn_latency.samples == 1 and 11.0 < vi._turn_latency.value() < 13.0, vi._turn_latency.value()
    vi._last_idle_beat_at = 0.0
    await vi._maybe_run_idle_beat()
    assert conn.budget == pytest.approx(40.0 + vi._turn_latency.value(), abs=0.5)


async def test_no_beat_while_a_walk_is_going_its_result_is_the_next_prompt(iface, rested, caplog) -> None:
    vi, reg, sent = iface
    now = asyncio.get_event_loop().time()
    conn = _Walker(ready=now - 10_000)
    conn.busy = True
    reg._instances["retroxr"] = conn
    await vi._maybe_run_idle_beat()
    assert sent == [], "she is on a walk"
    conn.busy = False
    await vi._maybe_run_idle_beat()
    assert len(sent) == 1


async def test_the_end_of_a_walk_counts_as_the_beat_it_replaces(iface, rested, monkeypatch: pytest.MonkeyPatch) -> None:
    vi, reg, sent = iface
    now = asyncio.get_event_loop().time()
    conn = _Walker(ready=now - 10_000)
    reg._instances["retroxr"] = conn

    class _Manager:
        async def start_session(self, *a: Any, **k: Any) -> str:
            return "s1"

        async def record_experience(self, *a: Any, **k: Any) -> None:
            pass

    async def no_log(*a: Any, **k: Any) -> None:
        pass

    monkeypatch.setattr("interface.vessel_interface.get_vessel_session_manager", lambda: _Manager())
    monkeypatch.setattr(vi, "_log_activity", no_log)
    monkeypatch.setattr(vi, "_is_salient", lambda *a, **k: False)
    vi._last_idle_beat_at = now - 500
    vi._idle_pending = False
    await vi.on_world_event("retroxr", "explore_done", "You went for a walk")
    assert vi._idle_pending is True and now - 5 < vi._last_idle_beat_at <= asyncio.get_event_loop().time()
    await vi.on_world_event("retroxr", "chat", "someone said hi")
    assert vi._last_idle_beat_at >= now - 5, "only a *_done does it"
