# plugins/rift_vessel/retroxr/retroxr.py
"""RetroXR connector for the Rift Vessel.

RetroXR is a VR retro-gaming room (Godot). Unlike Minecraft, where Synth starts
a Node bridge and polls it, the world here comes to Synth: the RetroXR mod
``xenu.synth_rift`` opens a WebSocket to ``/api/vessel/retroxr/ws`` when the
player switches on the *Synthetic Heart Rift Summoner*. That socket IS the
embodiment: events flow in as :class:`PerceptionEvent`, Synth's verbs flow out
as ``action`` frames and the mod answers each with an ``action_result``.

The body, its model and its animations are Synth's Karada assets, served over
the existing ``/api/karada`` HTTP routes; this module only decides which ones the
world is told about (``get_embodiment_assets``).

Wire protocol (JSON text frames, protocol 1)::

    mod  -> synth   {"type": "hello", "protocol": 1, "room": "...", "capabilities": [...]}
    mod  -> synth   (a mod that was welcomed before adds "resume": "<session>" to its hello)
    synth-> mod     {"type": "welcome", "session": "...", "resumed": false, "assets": {...}}
    mod  -> synth   {"type": "out"}              they stepped out of the capsule (with hello.early_summon the first
                                                 prompt, the ``summoned`` event, goes at the welcome, and a greeting made
                                                 for it is held by the mod until this moment)
    mod  -> synth   {"type": "bye"}              leaving on purpose: the session ends. A socket that just drops
                                                 does NOT end it: it waits out VESSEL_DISCONNECT_GRACE_SEC and a
                                                 hello with "resume" picks it up (a room change, a lost network)
    mod  -> synth   {"type": "forget"}           clear the remembered chat; synth-> mod {"type": "forgotten", "ok": true}
    mod  -> synth   {"type": "event", "event_type": "chat", "summary": "...", "actor": "...",
                     "salience": 0.7, "data": {...}}
    mod  -> synth   {"type": "state", "state": {"position": {...}, "extra": {...}}}
    mod  -> synth   {"type": "audio", "id": 3, "format": "wav", "data": "<base64>", "actor": "player"}
    synth-> mod     {"type": "transcript", "id": 3, "ok": true, "text": "...", "reason": ""}
    synth-> mod     {"type": "action", "id": 7, "action": "say", "payload": {...}}
    mod  -> synth   {"type": "action_result", "id": 7, "ok": true, "detail": "", "data": {}}

Constraints (docs/rift_vessel.rst): no agentic tasks, no diary mid-session. And
for RetroXR specifically: **the body never plays.** No verb may write input to a
core port; ``act`` refuses anything outside :data:`WORLD_VERBS` / the core set.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import itertools
import os
import tempfile
import time
from typing import Any, Dict

from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_info, log_warning
from core.plugin_base import PluginBase
from core.vessel_registry import register_vessel_connector
from plugins.rift_vessel.exploration import (
    Candidate,
    ExplorerMixin,
    Hazard,
    LookResult,
    Probe,
    Vec,
    WalkResult,
    as_vec,
    explore_schema,
    flat_dist,
)
from plugins.rift_vessel.karada_bridge import RiftKaradaTransport
from plugins.rift_vessel.vessel_base import (
    PerceptionCallback,
    PerceptionEvent,
    VesselActionResult,
    VesselConnectorBase,
    WorldState,
)

LOG_PREFIX = "[retroxr_connector]"
ENVIRONMENT = "retroxr"
PROTOCOL_VERSION = 1

def _queued_turns() -> int | str:
    """How many turns wait in the message queue (for the latency log; '?' when it cannot be read)."""
    try:
        from core import message_queue

        # The public snapshot: core_initializer refuses any plugin file that names the queue's internals.
        return len(message_queue.snapshot_queue(limit=5000))
    except Exception:
        return "?"


# How long a `say` holds its text for the first sentence of its voice before the text goes alone.
_SAY_HOLD_SEC = 8.0
#: What she did lately, kept for the idle prompt: how many entries, and how long each may be.
_JOURNAL_MAX = 12
_JOURNAL_TEXT_MAX = 110
#: A resumed session gets no ``out`` frame: she counts as ready this long after the mod attached.
_RESUME_READY_SEC = 45.0
# How long the mod has to answer one action frame.
_ACTION_TIMEOUT_SEC = 20.0
# Verbs that take a while in the room (walking across it, a carry).
_LONG_VERBS = frozenset({"move", "follow", "pick_up", "put_down", "hand_over", "sit"})
_LONG_ACTION_TIMEOUT_SEC = 90.0

# Her ears: a clip the player recorded in the room (mod -> synth `audio` frame), read by Auris.
_MAX_AUDIO_BYTES = 8 * 1024 * 1024
_AUDIO_MIME = {"wav": "audio/wav", "ogg": "audio/ogg", "opus": "audio/ogg", "mp3": "audio/mpeg", "flac": "audio/flac"}

# The core set (shared by every world) that makes sense inside a room.
CORE_VERBS = frozenset({"say", "move", "look", "use", "follow", "unfollow", "status"})

# RetroXR's own verbs: bare names, exposed as ``vessel_retroxr_<verb>``.
WORLD_VERBS: Dict[str, Dict[str, Any]] = {
    "look_at": {
        "description": (
            "Turn toward something in the room: an object, a machine, or the player. Give "
            "its name as it appears in what is around you (or \"player\")."
        ),
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
    "touch": {
        "description": (
            "Reach out and touch an object in the room, or the player's hand. "
            "This never presses a controller button: you do not play games."
        ),
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
    "search": {
        "description": (
            "Look for something in the room: turn all the way round, and if it is not in front of you "
            "walk to other places and look again, for up to about a minute. You see only what a person "
            "in front of you would. You are NOT asked anything meanwhile: you are told afterwards, as a "
            "new event, whether you found it (and where) or looked everywhere and it is not here. Use it "
            "when something is not in what is around you, instead of guessing where it is."
        ),
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
    "watch": {
        "description": (
            "Go to the TV (or another thing, with 'target') and watch it for a few seconds ('seconds', up to "
            "30). You are told afterwards what you saw there: whether it is on and, when the room can show "
            "it, a picture of the screen."
        ),
        "required_fields": [],
        "optional_fields": ["target", "seconds"],
        "security_level": "low",
    },
    "bring": {
        "description": (
            "Fetch an object for the player: find it (looking around if it is not in front of you), pick it up, "
            "walk to the player and hold it out until they take it. You are told afterwards how it went: "
            "brought, not found, or too heavy to carry."
        ),
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
    "stop": {
        "description": "Stop what you are doing on your own (looking, fetching, watching).",
        "required_fields": [],
        "optional_fields": [],
        "security_level": "low",
    },
    "pick_up": {
        "description": (
            "Walk up to a small object and pick it up in both hands so you can carry it (not furniture). "
            "You hold one object at a time. The player may take it from your hands. You are told afterwards "
            "that you are holding it."
        ),
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
    "put_down": {
        "description": "Put down what you are holding, in front of you, on whatever is under your hands (a table, the floor).",
        "required_fields": [],
        "optional_fields": ["target"],
        "security_level": "low",
    },
    "hand_over": {
        "description": (
            "Walk to the player and hold what you are carrying out so they can take it; if they do not, "
            "you put it down in front of them. You are told afterwards what happened."
        ),
        "required_fields": [],
        "optional_fields": [],
        "security_level": "low",
    },
    "sit": {
        "description": "Sit on a seat or a surface in the room; 'stand' as the target stands you up.",
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
    "emote": {
        "description": (
            "Play an animation from your skin: for example wave, laugh, think, "
            "greet. The animation must exist in your skin; unknown names are ignored."
        ),
        "required_fields": ["name"],
        "optional_fields": [],
        "security_level": "low",
    },
    "inspect": {
        "description": (
            "Go over to an object or machine and look at it closely, the way a person would: you walk up "
            "to it, and if it is small enough to lift (a cartridge, a console, a tape) you pick it up and "
            "study it in your hands; if it is too big or fixed (a TV, a wardrobe) you stand in front of it. "
            "You are told afterwards what it is and whether it is switched on, and whether you are holding "
            "it now (put_down or hand_over when you are done with it; looking closely at a second thing sets "
            "the first one down). Give its name as it appears in what is around you."
        ),
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
}


def _goal_store() -> Any:
    """The generic Goals plugin's module. (``plugins/goals/__init__.py`` swaps the package for the module in
    ``sys.modules``, so ``from plugins.goals import goals`` is an ImportError: import it by name.)"""
    import importlib

    return importlib.import_module("plugins.goals.goals")


# Verbs of Synth's own, never sent to the mod: they keep what she WANTS (the generic Goals plugin, scope
# ``vessel`` / ``retroxr``), not what her body does. Always offered, whatever the mod declared.
LOCAL_VERBS: Dict[str, Dict[str, Any]] = {
    "set_goal": {
        "description": (
            "Decide, in your own words, something you want to do here for a while, and keep it in mind: look "
            "closely at a machine, find out what is in the room, watch a game, tidy up, keep the player company. "
            "Nobody gives you this and nothing is on a list: it is only what you feel like doing. Put it in "
            "'description'. It stays your aim until you finish it or change your mind, so your next quiet moment "
            "starts from it instead of from nothing. Choosing a new one lets go of the old."
        ),
        "required_fields": ["description"],
        "optional_fields": ["note"],
        "security_level": "low",
    },
    "update_goal": {
        "description": (
            "Look back at the aim you set yourself: jot a 'note' on how it is going in your own words, set "
            "'status' to 'done' when you feel you have done it or 'abandoned' if you have lost interest. You are "
            "the judge of your own progress."
        ),
        "required_fields": [],
        "optional_fields": ["note", "status"],
        "security_level": "low",
    },
}

# `explore` is Synth's own too, since 2026-11-05: the core decides where to go and when to stop
# (plugins/rift_vessel/exploration.py) and asks the body for one thing at a time. Offered as every world offers it.
LOCAL_VERBS["explore"] = explore_schema(
    "You stop for a few seconds at things you have not looked at yet; the walk is not limited to the part of the room "
    "you can see from here."
)

# Verbs the CORE sends the mod to build a walk. They are never offered to the model and never accepted from it:
# `survey` answers what can be walked to and what is out of sight, `look_around` turns on the spot and names what comes
# into view. The mod declares them in `hello.verbs`; without `survey` the walk is blind (see ExplorationPolicy).
INTERNAL_VERBS = frozenset({"survey", "look_around"})

#: Her aims are kept per game, not per room: one house, one set of things she wants.
GOAL_SCOPE = {"scope": "vessel", "game": "retroxr", "world": "none"}


#: What a mod that says nothing about it (an old one) is taken to do.
DEFAULT_DECLARED = frozenset({"say", "move", "look", "status", "emote"})


class RetroXRConnector(ExplorerMixin, VesselConnectorBase):
    """Rift Vessel connector for the RetroXR room, over the mod's WebSocket."""

    display_name = "RetroXR"
    EXPLORE_WORLD = ENVIRONMENT
    # She is a person in a room, not a bot with goals: when nobody is talking to her she is
    # told what is around her and asked what she wants to do (core.vessel_beat idle beat).
    uses_idle_beat = True
    # A room is where she lives: what was said stays said on the next visit, until the player clears it from
    # the pod's menu (the mod's ``forget`` frame).
    remembers_chat = True

    def __init__(self) -> None:
        self._on_event: PerceptionCallback | None = None
        self._ws: Any = None
        self._connected = False
        self._connect_settings: Dict[str, Any] = {}
        self._hello: Dict[str, Any] = {}
        self._state: Dict[str, Any] = {}
        self._pending: Dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._send_lock = asyncio.Lock()
        # Karada only speaks to the mod once it has been welcomed (announce_state): the mod does not
        # listen before that, and a frame ahead of the welcome is one the handshake does not expect.
        self._welcomed = False
        self._karada = RiftKaradaTransport(self.send_frame, lambda: self.is_connected and self._welcomed)
        self._karada_registered = False
        self._speech_tasks: set[asyncio.Task] = set()
        self._voice_tasks: set[asyncio.Task] = set()
        # When the player's last chat arrived and has not been answered yet (for the latency log).
        self._chat_waiting_since: float | None = None
        self._emotes: list[str] = []
        self.last_error = None
        # The session id the mod was welcomed with (what its ``hello.resume`` must name to pick the session up
        # again), and whether the socket dropped WITHOUT a goodbye: the session then waits out the interface's
        # disconnect grace (VESSEL_DISCONNECT_GRACE_SEC) instead of ending, so a room change or a blip can resume.
        self.session_token: str | None = None
        self.paused = False
        # When she became ready to be asked what she wants (see ``idle_ready_since``), what she did lately, and
        # when the mod attached (a resumed session gets no ``out`` frame: the idle beat falls back to this).
        self._idle_ready_at: float | None = None
        self._attached_at: float | None = None
        # Where the body was at the last probe / walk (exploration measures a leg from it).
        self._explore_pos: Vec | None = None
        self._journal: "collections.deque[tuple[float, str]]" = collections.deque(maxlen=_JOURNAL_MAX)

    # ------------------------------------------------------------------
    # Socket ownership (driven by the route in plugins/rift_vessel/retroxr/api.py)
    # ------------------------------------------------------------------

    def attach_socket(self, ws: Any, hello: Dict[str, Any]) -> None:
        """Adopt the mod's socket. Called BEFORE ``connect_world`` runs."""
        self._ws = ws
        self._hello = dict(hello or {})
        self._state = {}
        self.last_error = None

    def owns(self, ws: Any) -> bool:
        """Is ``ws`` the socket this connector is attached to? (An old socket's handler must not touch a new one.)"""
        return ws is not None and ws is self._ws

    def pause_socket(self) -> None:
        """The socket dropped without a goodbye: let go of it but keep the session (see ``paused``)."""
        self.paused = True
        self.cancel_explore("the rift dropped", silent=True)
        self.detach_socket()
        log_info(f"{LOG_PREFIX} socket lost: the session {self.session_token} waits for the mod to come back")

    def detach_socket(self) -> None:
        """Forget the socket and fail every action still waiting for an answer."""
        self._unregister_karada()
        self._welcomed = False
        self._ws = None
        self._connected = False
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(VesselActionResult(ok=False, detail="rift_closed"))
        self._pending.clear()

    @property
    def is_connected(self) -> bool:
        return bool(self._connected and self._ws is not None)

    def declared_verbs(self) -> frozenset[str]:
        """The verbs the mod said it can carry out (``hello.verbs``), within what this connector knows."""
        raw = self._hello.get("verbs")
        if isinstance(raw, list):
            return frozenset(str(v) for v in raw) & (CORE_VERBS | frozenset(WORLD_VERBS))
        return DEFAULT_DECLARED

    @property
    def core_verbs(self) -> frozenset[str]:  # type: ignore[override]
        """Core verbs Synth is shown: only those the mod can do. Nothing she cannot do is offered."""
        return CORE_VERBS & self.declared_verbs()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(
        self, settings: Dict[str, Any], on_event: PerceptionCallback
    ) -> bool:
        self._connect_settings = dict(settings or {})
        if self._ws is None and self.paused:
            # The interface's reconnect sweep asks every few seconds: the mod's own hello is the way back in.
            log_debug(f"{LOG_PREFIX} paused: waiting for the mod to resume")
            return False
        if self._ws is None:
            self.last_error = (
                "the RetroXR rift is closed: switch on the Synthetic Heart Rift "
                "Summoner in the room to open it"
            )
            log_info(f"{LOG_PREFIX} connect refused: no socket attached")
            return False
        self._on_event = on_event
        self._connected = True
        self.paused = False
        self._idle_ready_at = None
        self._attached_at = time.monotonic()
        await self._register_karada()
        log_info(
            f"{LOG_PREFIX} embodied (room={self._hello.get('room')!r}, "
            f"caps={self._hello.get('capabilities')})"
        )
        return True

    async def disconnect(self) -> None:
        self.paused = False
        self.session_token = None
        self._journal.clear()
        self._idle_ready_at = None
        self.reset_exploration()
        ws = self._ws
        self._unregister_karada()
        self.detach_socket()
        self._on_event = None
        if ws is not None:
            try:
                await ws.close()
            except Exception as exc:
                log_debug(f"{LOG_PREFIX} close on disconnect: {exc}")
        log_info(f"{LOG_PREFIX} disconnected")

    def get_world_identity(self) -> str | None:
        """One scope for the whole house: ``vessel/retroxr``.

        It was per room, which cut her chat (and a session resumed after a room change) off from the room she
        came from. The room is a thing she is told about, not who she is talking in front of.
        """
        return None

    async def send_frame(self, frame: Dict[str, Any]) -> bool:
        """Send one JSON frame to the mod. False when the rift is not open or the send failed."""
        ws = self._ws
        if ws is None or not self._connected:
            return False
        try:
            async with self._send_lock:
                await ws.send_json(frame)
            return True
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} send failed ({frame.get('type')}): {exc}")
            return False

    # ------------------------------------------------------------------
    # Karada: her animation states, voice and face follow the avatar
    # ------------------------------------------------------------------

    async def _register_karada(self) -> None:
        """Add the rift as a Karada transport (everything Karada says from now on reaches the mod)."""
        try:
            from core.animation_handler import get_karada_state_server

            karada = get_karada_state_server()
            karada.add_transport(self._karada)
            self._karada_registered = True
            # She is being introduced into the room: Karada's presence says so until the mod
            # reports that she has stepped out (the `summoned` event) or the rift closes.
            if hasattr(karada, "set_presence"):
                await karada.set_presence("summon", "retroxr")
            log_info(f"{LOG_PREFIX} registered as a Karada transport")
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} could not register with Karada: {exc}")

    async def announce_state(self) -> None:
        """Tell the mod what she is doing right now, not only what changes next.

        Called by the rift's socket handler AFTER the welcome: the mod only listens for Karada
        once it has been welcomed.
        """
        self._welcomed = True
        try:
            from core.animation_handler import get_karada_state_server

            karada = get_karada_state_server()
            current = karada.get_current_animation_state()
            if isinstance(current, dict) and current.get("state"):
                await self._karada.broadcast_animation(
                    {"type": "vrm_animation_v2", "state": current.get("state")}
                )
            presence = karada.get_presence() if hasattr(karada, "get_presence") else None
            if presence:
                await self._karada.broadcast_presence({"type": "vrm_presence", "state": presence})
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} announce_state: {exc}")

    def _unregister_karada(self) -> None:
        if not self._karada_registered:
            return
        self._karada_registered = False
        try:
            from core.animation_handler import get_karada_state_server

            karada = get_karada_state_server()
            karada.remove_transport(self._karada)
            self._release_summon_nowait(karada)
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} karada unregister: {exc}")

    async def _release_summon(self) -> None:
        """She has arrived (or the rift closed): she is no longer being introduced."""
        if self._idle_ready_at is None and self.is_connected:
            self._idle_ready_at = time.monotonic()
            log_info(f"{LOG_PREFIX} she is out and ready to be asked what she wants to do")
        try:
            from core.animation_handler import get_karada_state_server

            karada = get_karada_state_server()
            if hasattr(karada, "get_presence") and karada.get_presence() == "summon":
                await karada.set_presence(None, "retroxr")
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} release summon: {exc}")

    def _release_summon_nowait(self, karada: Any) -> None:
        if not hasattr(karada, "get_presence") or karada.get_presence() != "summon":
            return
        try:
            asyncio.get_running_loop().create_task(karada.set_presence(None, "retroxr"))
        except RuntimeError:  # no loop: nothing can be listening either
            pass

    # ------------------------------------------------------------------
    # Voice: in RetroXR she speaks as well as writes
    # ------------------------------------------------------------------

    async def _speak(self, text: str, first_ready: "asyncio.Event | None" = None) -> None:
        """Synthesize ``text`` with Vox, sentence by sentence, and hand each clip to Karada (every transport, the rift too).

        The whole reply used to be synthesized before the first byte of it was heard, so a long answer's voice came
        seconds after its text (the WebUI avatar streams per sentence and was heard first). Now the first sentence is
        broadcast the moment it is ready while the next one synthesizes behind it, the clips are paced by their own
        length (Karada's talk animation follows the last broadcast) and the mod plays them in order. Each sentence
        carries its own ``[em_*]`` tags, so its face timeline starts with its clip.

        ``first_ready`` is set when the first clip has been broadcast, or when there will be none (no voice engine, a
        failure): :meth:`act` holds the text for it so that text and voice appear together. Fully fail-safe: no voice
        engine means the line stays text.
        """
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            vox = PLUGIN_REGISTRY.get("vox_plugin")
            if vox is None or not hasattr(vox, "speak"):
                log_debug(f"{LOG_PREFIX} no Vox plugin: the line stays text")
                return
            from core.animation_handler import get_karada_state_server
            from core.facial_expression_parser import parse_facial_expressions

            try:
                from plugins.vox_plugin.vox_plugin import _split_sentences

                pieces = _split_sentences(text) or [text]
            except Exception:  # pragma: no cover - the splitter is Vox's, whole text is the safe fallback
                pieces = [text]
            karada = get_karada_state_server()

            async def synth(piece: str) -> Any:
                try:
                    return await vox.speak(piece, interface_path=None, generate_only=True, allow_fallback=False)
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} synthesizing a sentence failed: {exc}")
                    return None

            started = time.monotonic()
            nxt: "asyncio.Task | None" = asyncio.create_task(synth(pieces[0]))
            for i, piece in enumerate(pieces):
                result = await nxt  # type: ignore[misc]
                nxt = asyncio.create_task(synth(pieces[i + 1])) if i + 1 < len(pieces) else None
                if not isinstance(result, dict) or result.get("status") != "success":
                    log_debug(f"{LOG_PREFIX} Vox did not speak sentence {i + 1}/{len(pieces)}: {result}")
                    continue
                clean, em_events = parse_facial_expressions(piece)
                duration = result.get("audio_duration_s")
                await karada.broadcast_audio(
                    audio_path=str(result["audio_path"]),
                    lipsync_data=result.get("lipsync_data"),
                    audio_duration_s=duration,
                    text=clean or piece,
                )
                if i == 0:
                    log_info(
                        f"{LOG_PREFIX} first sentence of {len(pieces)} on its way after "
                        f"{time.monotonic() - started:.1f}s"
                    )
                    if first_ready is not None:
                        first_ready.set()
                # Her face follows the [em_*] tags in what she said, as it does on the WebUI.
                if em_events and hasattr(vox, "_schedule_expression_timeline"):
                    vox._schedule_expression_timeline(em_events, clean, None, duration)
                if nxt is not None and duration:
                    await asyncio.sleep(float(duration))  # the next clip is already being made behind this one
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} speaking failed: {exc}")
        finally:
            if first_ready is not None:
                first_ready.set()

    def _speak_in_background(self, text: str, first_ready: "asyncio.Event | None" = None) -> None:
        task = asyncio.get_running_loop().create_task(self._speak(text, first_ready))
        self._speech_tasks.add(task)
        task.add_done_callback(self._speech_tasks.discard)

    # ------------------------------------------------------------------
    # The idle beat: what is around her
    # ------------------------------------------------------------------

    def idle_ready_since(self) -> float | None:
        """When she was out of the capsule and could be asked what she wants (``time.monotonic()``), else ``None``."""
        if not self.is_connected or not self._state:
            return None
        if self._idle_ready_at is not None:
            return self._idle_ready_at
        if self._attached_at is not None and time.monotonic() - self._attached_at >= _RESUME_READY_SEC:
            # No ``out`` frame came (a resumed session, an older mod): count from when the mod attached.
            return self._attached_at + _RESUME_READY_SEC
        return None

    def idle_journal(self) -> "list[tuple[float, str]]":
        return list(self._journal)

    def describe_surroundings(self) -> str | None:
        """What she can see around her, in words, for "what do you want to do?"."""
        if not self.is_connected:
            return None
        if not self._state:
            # Her eyes have reported nothing yet: "you cannot see the player, there is nothing near you" would be a
            # lie told to the model (it wasted the first beat, 2026-11-04), so there is nothing to ask about.
            return None
        st = self._state or {}
        entities = [e for e in ((st.get("extra") or {}).get("entities") or []) if isinstance(e, dict)]
        lines: list[str] = []
        player = next((e for e in entities if e.get("type") == "player"), None)
        if player:
            lines.append(
                f"The player is {player.get('side', 'near you')}, "
                f"{float(player.get('distance', 0)):.1f} m away."
            )
        else:
            lines.append("You cannot see the player.")
        things = [e for e in entities if e.get("type") != "player"]
        if things:
            lines.append("Around you:")
            for e in things[:12]:
                label = str(e.get("label") or e.get("name") or "something")
                extra = []
                if e.get("systemid"):
                    extra.append(f"a {e['systemid']} machine")
                if e.get("powered") is True:
                    extra.append("switched on")
                elif e.get("powered") is False:
                    extra.append("switched off")
                suffix = f" ({', '.join(extra)})" if extra else ""
                lines.append(
                    f"- {label}{suffix}, {e.get('side', 'near you')}, {float(e.get('distance', 0)):.1f} m"
                )
        else:
            lines.append("There is nothing else near you.")
        holding = st.get("holding")
        lines.append(f"You are holding {holding}." if holding else "Your hands are empty.")
        if self._emotes:
            lines.append("Animations you can play: " + ", ".join(self._emotes) + ".")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Inbound frames
    # ------------------------------------------------------------------

    async def handle_frame(self, msg: Dict[str, Any]) -> None:
        """Dispatch one frame received from the mod."""
        kind = str(msg.get("type") or "")
        if kind == "action_result":
            fut = self._pending.pop(int(msg.get("id") or 0), None)
            if fut is not None and not fut.done():
                fut.set_result(
                    VesselActionResult(
                        ok=bool(msg.get("ok")),
                        detail=msg.get("detail") or None,
                        data=msg.get("data") if isinstance(msg.get("data"), dict) else {},
                    )
                )
            return
        if kind == "state":
            state = msg.get("state")
            if isinstance(state, dict):
                self._state = state
            return
        if kind == "event":
            await self._dispatch_event(msg)
            return
        if kind == "out":
            # They have stepped out of the Summoner: the introduction is over (the first prompt went at the welcome).
            await self._release_summon()
            return
        if kind == "forget":
            # The pod's menu: clear what has been said. The mod clears its own log; this is Synth's.
            ok = await self.forget_memory(ENVIRONMENT, self.get_world_identity())
            log_info(f"{LOG_PREFIX} the player asked her to forget the chat: {'done' if ok else 'nothing to clear'}")
            await self.send_frame({"type": "forgotten", "ok": ok})
            return
        if kind == "audio":
            # Transcribing takes a while: never inside the read loop, or the socket stops being read.
            task = asyncio.create_task(self._hear(msg))
            self._voice_tasks.add(task)
            task.add_done_callback(self._voice_tasks.discard)
            return
        log_debug(f"{LOG_PREFIX} ignored frame type {kind!r}")

    async def _hear(self, msg: Dict[str, Any]) -> None:
        """The player spoke to her: a clip from the mod becomes a normal ``chat`` event.

        Auris reads it (whatever engine the player chose); the text then travels exactly as typed text does, so
        salience, priority and history treat both alike. The mod is told the transcript (to show it in the chat page)
        or why there is none, so a press on the mic is never answered with silence.
        """
        clip_id = msg.get("id")

        async def _answer(ok: bool, text: str = "", reason: str = "") -> None:
            await self.send_frame({"type": "transcript", "id": clip_id, "ok": ok, "text": text, "reason": reason})

        fmt = str(msg.get("format") or "wav").lower()
        mime = _AUDIO_MIME.get(fmt)
        raw = msg.get("data")
        if mime is None or not isinstance(raw, str) or not raw:
            log_warning(f"{LOG_PREFIX} audio clip {clip_id!r} refused: format={fmt!r}, data={'yes' if raw else 'no'}")
            await _answer(False, reason="bad_clip")
            return
        try:
            audio = base64.b64decode(raw, validate=False)
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} audio clip {clip_id!r} is not base64: {exc}")
            await _answer(False, reason="bad_clip")
            return
        if not audio or len(audio) > _MAX_AUDIO_BYTES:
            log_warning(f"{LOG_PREFIX} audio clip {clip_id!r} refused: {len(audio)} bytes (max {_MAX_AUDIO_BYTES})")
            await _answer(False, reason="too_long" if audio else "bad_clip")
            return

        try:
            from core.core_initializer import PLUGIN_REGISTRY

            auris = PLUGIN_REGISTRY.get("auris_plugin")
        except Exception:  # pragma: no cover - defensive
            auris = None
        if auris is None or not hasattr(auris, "transcribe_audio"):
            log_warning(f"{LOG_PREFIX} audio clip {clip_id!r}: no Auris plugin, nothing can hear it")
            await _answer(False, reason="no_stt")
            return

        path = ""
        try:
            fd, path = tempfile.mkstemp(prefix="retroxr_voice_", suffix="." + fmt)
            with os.fdopen(fd, "wb") as fh:
                fh.write(audio)
            started = time.monotonic()
            result = await auris.transcribe_audio(path, mime)
            text = str(getattr(result, "text", "") or "").strip()
            log_info(
                f"{LOG_PREFIX} clip {clip_id!r}: {len(audio)} bytes -> {len(text)} chars "
                f"in {time.monotonic() - started:.1f}s (lang={getattr(result, 'language', None)!r})"
            )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} audio clip {clip_id!r}: transcription failed: {exc}")
            await _answer(False, reason="stt_failed")
            return
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if not text:
            await _answer(False, reason="nothing_heard")
            return

        actor = str(msg.get("actor") or "player")
        await _answer(True, text=text)
        await self._dispatch_event(
            {
                "event_type": "chat",
                "summary": f"{actor}: {text}",
                "actor": actor,
                "salience": 1.0,
                "data": {"message": text, "addressed": True, "via": "voice"},
            }
        )

    async def _dispatch_event(self, raw: Dict[str, Any]) -> None:
        if str(raw.get("event_type") or "") == "chat" and raw.get("actor"):
            # A person talking to her ends a walk at its next safe point: they are the next prompt, not the walk's end.
            self.cancel_explore("the player spoke", silent=True)
            self._chat_waiting_since = time.monotonic()
            log_info(f"{LOG_PREFIX} player chat in (queued turns: {_queued_turns()})")
        if str(raw.get("event_type") or "").endswith("_done"):
            # A routine she started has ended (search/explore/watch/bring...): that is part of what she did.
            self._note(str(raw.get("summary") or raw.get("event_type")))
        if str(raw.get("event_type") or "") == "summoned" and not self._hello.get("early_summon"):
            # A mod that sends the first prompt EARLY (``hello.early_summon``) says when they are out with an ``out``
            # frame; an older one has no such frame, so its ``summoned`` event, sent once they are out, ends it.
            await self._release_summon()
        if self._on_event is None:
            return
        try:
            data = dict(raw.get("data")) if isinstance(raw.get("data"), dict) else {}
            # A picture from her eyes (the mod sends a JPEG as base64): it travels as an attachment, the shape
            # every interface's attachments take, so the model can read it with this turn.
            image = data.pop("image_jpeg_b64", None)
            if isinstance(image, str) and image:
                data["attachments"] = [{"mime_type": "image/jpeg", "data": image, "filename": "what_she_saw.jpg"}]
            event = PerceptionEvent(
                environment=ENVIRONMENT,
                event_type=str(raw.get("event_type") or "event"),
                summary=str(raw.get("summary") or ""),
                actor=raw.get("actor"),
                salience=raw.get("salience"),
                data=data,
            )
            result = self._on_event(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} failed to dispatch event: {exc}")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def act(self, action: str, payload: Dict[str, Any]) -> VesselActionResult:
        result = await self._act(action, payload)
        self._journal_action(str(action or ""), payload, result)
        return result

    def _note(self, text: str) -> None:
        """Remember one thing she did (or that happened to her routine) for the idle prompt."""
        text = " ".join(str(text or "").split())
        if text:
            self._journal.append((time.monotonic(), text[:_JOURNAL_TEXT_MAX]))

    def _journal_action(self, verb: str, payload: Dict[str, Any] | None, result: VesselActionResult) -> None:
        """Journal a verb she chose. ``status`` reads a number and is not something she did."""
        if verb == "status":
            return
        payload = payload if isinstance(payload, dict) else {}
        if verb == "say":
            what = f'said "{str(payload.get("text") or "").strip()}"'
        else:
            target = str(payload.get("target") or payload.get("name") or "").strip()
            what = f"{verb} {target}".strip()
        if not result.ok:
            what += f" (did not work: {result.detail or 'no reason given'})"
        self._note(what)

    async def _act_goal(self, verb: str, payload: Dict[str, Any] | None) -> VesselActionResult:
        """``set_goal`` / ``update_goal``: what she wants, kept in the generic Goals plugin (never sent to the mod)."""
        payload = payload if isinstance(payload, dict) else {}
        try:
            goal_store = _goal_store()

            if verb == "set_goal":
                description = ""
                for key in ("description", "goal", "goal_text", "objective", "intention"):
                    if isinstance(payload.get(key), str) and payload[key].strip():
                        description = payload[key].strip()
                        break
                if not description:
                    log_warning(f"{LOG_PREFIX} set_goal refused: no description in {sorted(payload)}")
                    return VesselActionResult(ok=False, detail="set_goal requires a free-text description")
                result = await goal_store.set_goal(
                    description, self.session_token, note=payload.get("note"), **GOAL_SCOPE
                )
            else:
                result = await goal_store.update_active_goal(
                    note=payload.get("note"), status=payload.get("status"), **GOAL_SCOPE
                )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} {verb} failed: {exc}")
            return VesselActionResult(ok=False, detail=f"goals_unavailable: {exc}")
        ok = result.get("status") == "ok"
        log_info(f"{LOG_PREFIX} {verb} -> {'ok' if ok else 'refused'} ({result.get('message') or ''})")
        return VesselActionResult(ok=ok, detail=result.get("message") or ("goal updated" if ok else "refused"), data=result)

    async def idle_intention(self) -> Dict[str, Any] | None:
        """The aim she set herself and has not finished, for the idle prompt (``None`` when she has none)."""
        try:
            goal_store = _goal_store()

            return await goal_store.get_active_goal(**GOAL_SCOPE)
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} idle_intention unavailable: {exc}")
            return None

    async def _act(self, action: str, payload: Dict[str, Any]) -> VesselActionResult:
        verb = str(action or "")
        if verb == "explore":
            if not self.is_connected:
                return VesselActionResult(ok=False, detail="rift_closed")
            started = self.start_explore(payload)
            return VesselActionResult(ok=bool(started["ok"]), detail=started["detail"], data=started["data"])
        if verb in LOCAL_VERBS:
            return await self._act_goal(verb, payload)
        if verb not in CORE_VERBS and verb not in WORLD_VERBS:
            # The body never plays: nothing outside the declared verbs reaches the room.
            log_warning(f"{LOG_PREFIX} refused unknown verb {verb!r}")
            return VesselActionResult(ok=False, detail=f"unknown_verb: {verb}")
        if not self.is_connected:
            return VesselActionResult(ok=False, detail="rift_closed")
        if verb not in self.declared_verbs():
            # Known, but this mod cannot do it: say so instead of letting the mod fail it.
            log_warning(f"{LOG_PREFIX} refused {verb!r}: the mod did not declare it")
            return VesselActionResult(ok=False, detail=f"not_available: {verb}")

        if verb == "say":
            if self._chat_waiting_since is not None:
                log_info(f"{LOG_PREFIX} answered the player after {time.monotonic() - self._chat_waiting_since:.1f}s")
                self._chat_waiting_since = None
            # In RetroXR she speaks as well as writes, always: the text is the caption of the voice.
            payload = dict(payload or {})
            payload["audio"] = True
            spoken = str(payload.get("text") or "").strip()
            if spoken:
                # The text waits for the first sentence of the voice (a few seconds at most), so that what is
                # written and what is heard arrive together instead of the voice trailing the text.
                first_ready = asyncio.Event()
                held = time.monotonic()
                self._speak_in_background(spoken, first_ready)
                try:
                    await asyncio.wait_for(first_ready.wait(), timeout=_SAY_HOLD_SEC)
                except asyncio.TimeoutError:
                    log_warning(f"{LOG_PREFIX} the voice was not ready in {_SAY_HOLD_SEC:.0f}s: the text goes first")
                else:
                    log_info(f"{LOG_PREFIX} the text was held {time.monotonic() - held:.1f}s for its voice")
                # The caption is what is said: no [em_*] tags in a speech bubble or the chat page.
                try:
                    from core.facial_expression_parser import parse_facial_expressions

                    payload["text"] = parse_facial_expressions(spoken)[0] or spoken
                except Exception:  # pragma: no cover - defensive
                    pass

        return await self._roundtrip(verb, payload)

    async def _roundtrip(self, verb: str, payload: Dict[str, Any] | None, timeout: float | None = None) -> VesselActionResult:
        """Send one action frame to the mod and wait for its answer."""
        if not self.is_connected:
            return VesselActionResult(ok=False, detail="rift_closed")
        action_id = next(self._ids)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[action_id] = fut
        try:
            async with self._send_lock:
                await self._ws.send_json(
                    {
                        "type": "action",
                        "id": action_id,
                        "action": verb,
                        "payload": dict(payload or {}),
                    }
                )
        except Exception as exc:
            self._pending.pop(action_id, None)
            self._connected = False
            self.last_error = f"lost the RetroXR socket: {exc}"
            log_warning(f"{LOG_PREFIX} send failed for {verb!r}: {exc}")
            return VesselActionResult(ok=False, detail="rift_closed")

        if timeout is None:
            timeout = _LONG_ACTION_TIMEOUT_SEC if verb in _LONG_VERBS else _ACTION_TIMEOUT_SEC
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(action_id, None)
            log_warning(f"{LOG_PREFIX} {verb!r} not answered in {timeout:.0f}s")
            return VesselActionResult(ok=False, detail="timeout")

    # ------------------------------------------------------------------
    # Exploration: what the core asks of a body (plugins/rift_vessel/exploration.py)
    # ------------------------------------------------------------------

    def mod_supports(self, verb: str) -> bool:
        """Did the mod declare ``verb`` (also the INTERNAL ones, which ``declared_verbs`` leaves out)?"""
        raw = self._hello.get("verbs")
        return isinstance(raw, list) and verb in raw

    def _body_position(self) -> Vec | None:
        return as_vec((self._state or {}).get("position"))

    async def explore_probe(self, viewpoints, max_candidates):  # type: ignore[override]
        if not self.is_connected:
            return None
        here = self._body_position()
        if not self.mod_supports("survey"):
            if here is None:
                return None
            self._explore_pos = here
            # Blind: the mod cannot say what can be walked to, so the core makes places up and a failed walk marks them.
            return Probe(position=here, heading=None, candidates=None)
        res = await self._roundtrip(
            "survey", {"viewpoints": [list(v) for v in viewpoints], "max": int(max_candidates)}, timeout=_ACTION_TIMEOUT_SEC
        )
        if not res.ok:
            log_warning(f"{LOG_PREFIX} survey failed: {res.detail}")
            return None
        d = res.data or {}
        position = as_vec(d.get("position")) or here
        if position is None:
            return None
        self._explore_pos = position
        cands, pois, hazards = [], [], []
        for c in d.get("candidates") or []:
            p = as_vec(c.get("pos")) if isinstance(c, dict) else None
            if p is not None:
                cands.append(Candidate(pos=p, cost=float(c.get("cost") or 0.0), seen=bool(c.get("seen"))))
        for c in d.get("pois") or []:
            p = as_vec(c.get("pos")) if isinstance(c, dict) else None
            if p is not None and c.get("name"):
                pois.append(Candidate(pos=p, cost=float(c.get("cost") or 0.0), seen=bool(c.get("seen")), kind="poi",
                                      name=str(c["name"]), area=c.get("area")))
        for h in d.get("hazards") or []:
            p = as_vec(h.get("pos")) if isinstance(h, dict) else None
            if p is not None:
                hazards.append(Hazard(pos=p, radius=float(h.get("radius") or 0.8), kind=str(h.get("kind") or "obstacle")))
        heading = d.get("heading")
        return Probe(position=position, heading=float(heading) if isinstance(heading, (int, float)) else None,
                     candidates=cands, pois=pois, hazards=hazards)

    async def explore_walk(self, pos, timeout):  # type: ignore[override]
        before = self._explore_pos or self._body_position()
        res = await self._roundtrip(
            "move", {"x": pos[0], "y": pos[1], "z": pos[2], "free": True}, timeout=max(10.0, float(timeout) + 5.0)
        )
        after = as_vec((res.data or {}).get("position"))
        moved = flat_dist(before, after) if before is not None and after is not None else 0.0
        if after is not None:
            self._explore_pos = after
        return WalkResult(ok=bool(res.ok), detail=str(res.detail or ""), position=after, moved=moved)

    async def explore_look(self, seconds):  # type: ignore[override]
        if not self.mod_supports("look_around"):
            await self.explore_sleep(min(float(seconds), 6.0))
            return LookResult(saw=[])
        res = await self._roundtrip("look_around", {"seconds": float(seconds)}, timeout=float(seconds) + 25.0)
        saw = []
        for item in (res.data or {}).get("saw") or []:
            if isinstance(item, dict) and item.get("name"):
                saw.append({"name": str(item["name"]), "area": item.get("area")})
        return LookResult(saw=saw)

    async def explore_study(self, poi):  # type: ignore[override]
        # A look, not an `inspect`: that one now walks up to a thing and picks it up as a routine of its own, which would
        # fight the walk this is a stop of.
        if not self.mod_supports("look_at") or not poi.name:
            return True
        res = await self._roundtrip("look_at", {"target": poi.name})
        return bool(res.ok)

    async def explore_halt(self):  # type: ignore[override]
        if self.is_connected and self.mod_supports("stop"):
            await self._roundtrip("stop", {}, timeout=5.0)

    # ------------------------------------------------------------------
    # World state and capabilities
    # ------------------------------------------------------------------

    async def get_world_state(self) -> WorldState | None:
        if not self.is_connected:
            return None
        st = self._state or {}
        extra = dict(st.get("extra") or {})
        extra.setdefault("room", self._hello.get("room"))
        return WorldState(
            environment=ENVIRONMENT,
            health=None,
            position=st.get("position"),
            possible_actions=sorted(self.declared_verbs()),
            flags={"connected": True, "holding": st.get("holding")},
            extra=extra,
        )

    def get_world_actions(self) -> Dict[str, Dict[str, Any]]:
        declared = self.declared_verbs()
        actions = {verb: dict(schema) for verb, schema in WORLD_VERBS.items() if verb in declared}
        actions.update({verb: dict(schema) for verb, schema in LOCAL_VERBS.items()})
        return actions

    def describe_capabilities(self) -> Dict[str, Any]:
        return {
            "movement": True,
            "chat": True,
            "perception": True,
            "interaction": True,
            "gameplay": False,
            "mod_capabilities": list(self._hello.get("capabilities") or []),
        }

    async def get_embodiment_assets(
        self, wanted_animations: list[str] | None = None
    ) -> Dict[str, Any] | None:
        """What the world is told about Synth's body (Rift Vessel extension).

        The mod fetches the files over ``/skins/...`` itself; here Synth names the
        skin and resolves the animation names RetroXR asked for. Nothing is pushed.
        """
        from plugins.rift_vessel.embodiment_assets import describe_assets

        wanted = wanted_animations
        if wanted is None:
            wanted = list(self._hello.get("animations") or [])
        body = await describe_assets(
            wanted_animations=wanted,
        )
        try:
            self._emotes = sorted((body or {}).get("animations", {}).keys())
        except Exception:  # pragma: no cover - defensive
            pass
        # Who they are called: the current persona's name, so the mod's chat can label their lines with it
        # (absent when no persona is loaded: the mod then says "SyntH").
        persona_name = self._persona_name()
        if body is not None and persona_name:
            body["name"] = persona_name
        return body

    @staticmethod
    def _persona_name() -> str:
        """The current persona's name, or "" when there is none (never raises)."""
        try:
            from core.persona_manager import get_persona_manager

            persona = get_persona_manager().get_current_persona()
            return str(getattr(persona, "name", "") or "").strip()
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} could not read the persona name: {exc}")
            return ""


CONNECTOR_CLASS = RetroXRConnector

register_vessel_connector(
    "retroxr",
    __name__,
    capabilities={
        "movement": True,
        "chat": True,
        "perception": True,
        "interaction": True,
        "local": False,
    },
    label="RetroXR (Rift Summoner)",
)


class RetroXRVesselPlugin(PluginBase):
    """Attachable RetroXR Vessel sub-plugin (toggleable like Minecraft's).

    Registers no actions: the ``vessel_*`` actions live in the Rift Vessel core
    plugin. It surfaces the RetroXR world as its own card and owns its keys.
    """

    display_name = "RetroXR Vessel"

    def __init__(self) -> None:
        super().__init__()
        register_plugin("retroxr_vessel", self)
        log_info("[retroxr_vessel] Registered RetroXRVesselPlugin")

    def get_metadata(self) -> dict:
        return {
            "name": "retroxr_vessel",
            "display_name": "RetroXR Vessel",
            "description": (
                "Lets Synth step into a RetroXR room through the Synthetic Heart "
                "Rift Summoner (the RetroXR mod xenu.synth_rift). The mod connects to "
                "this server; Synth moves, speaks and handles objects in the room, "
                "but never plays games. Requires the Rift Vessel core plugin."
            ),
            "category": "Vessels",
            "icon": "icon.svg",
            "guide": "guide.md",
            "depends_on": ["vessel_plugin"],
        }

    def get_supported_action_types(self) -> list[str]:
        return []

    def get_supported_actions(self) -> dict:
        return {}

    async def teardown(self) -> None:
        """Close the rift when this sub-plugin is switched off. Never raises."""
        try:
            from core.vessel_registry import VESSEL_REGISTRY

            connector = (getattr(VESSEL_REGISTRY, "_instances", {}) or {}).get(
                ENVIRONMENT
            )
            if connector is not None:
                await connector.disconnect()
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[retroxr_vessel] teardown: disconnect failed: {exc}")
        try:
            from core.core_initializer import INTERFACE_REGISTRY

            iface = INTERFACE_REGISTRY.get("vessel")
            if iface is not None and hasattr(iface, "end_sessions_for_environment"):
                await iface.end_sessions_for_environment(ENVIRONMENT, reason="logout")
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[retroxr_vessel] teardown: end session failed: {exc}")
        log_info("[retroxr_vessel] teardown complete (rift closed on disable)")


PLUGIN_CLASS = RetroXRVesselPlugin
