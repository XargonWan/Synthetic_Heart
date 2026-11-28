"""A call held by a Live engine: the Core's side of a full-duplex conversation.

The classic call path (``core.call_audio``) listens, transcribes, answers with the normal
turn pipeline and speaks the reply. A Live engine does all of that in one model session, so
what is left for the Core is the part that has nothing to do with any model:

* it opens the session with the persona and the tools (``prompt_engine``, ``live_tool_registry``),
* it feeds the caller's sound in and hands the model's sound back to the interface that carries
  the call (a :class:`LiveSink`),
* it runs the model's tool calls through the action pipeline,
* it writes both sides of every turn to the chat history and to memory, and a diary entry when
  the call ends,
* it drops what is queued to play when the caller speaks over the model.

Nothing here knows which provider is behind the engine: it talks ``LiveEngineBase``, whether
that is in-tree or a ``RemoteLiveEngine`` served by an extension (``core.extensions.live``).
The interface that carries the call (``core.extensions.calls``) is the sink.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Protocol

from core.logging_utils import log_debug, log_info, log_warning
from plugins.live_base import LiveEngineBase, LiveEvent, LiveEventType

#: Model audio is 24 kHz mono unless the engine says otherwise.
DEFAULT_OUTPUT_RATE = 24000
#: Consecutive engine errors after which the call is given up.
MAX_ERRORS = 5
TOOL_TIMEOUT = 20.0


class LiveSink(Protocol):
    """Where the model's voice goes: the interface carrying the call."""

    async def stream(self, pcm: bytes, rate: int) -> None:
        """Play this 16-bit mono PCM into the call, after what is already queued."""
        ...

    async def flush(self) -> None:
        """Drop everything queued but not yet played (the caller spoke over the model)."""
        ...


@dataclass
class BridgePorts:
    """What the bridge needs from the Core, injectable for tests."""

    #: ``() -> (name, engine)`` of the Live engine to use, or ``None``.
    resolve_engine: Callable[[], tuple[str, LiveEngineBase] | None]
    system_instruction: Callable[[], Awaitable[str]]
    #: Neutral tool declarations: ``[{"name", "description", "parameters": [...]}]``.
    tools: Callable[[], list[dict[str, Any]]]
    #: ``(name, args, context) -> result`` through the action pipeline.
    run_tool: Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]
    save_message: Callable[..., Awaitable[Any]]
    record_memory: Callable[[str, str], Awaitable[None]]
    record_diary: Callable[[str, str, list[tuple[str, str]]], Awaitable[None]]
    #: ``() -> (voice, language)`` the operator configured for live sessions.
    voice: Callable[[], tuple[str, str]] = lambda: ("", "")


def _tidy(parts: list[str]) -> str:
    """Fragments of one side of a turn as a sentence, without the persona's emotion tags
    (``{emotion neutral intensity 3.0}``) the model sometimes speaks back."""
    text = re.sub(r"\{[^}]*\}", "", " ".join(parts))
    return re.sub(r"\s{2,}", " ", text).strip()


@dataclass
class _Turn:
    user: list[str] = field(default_factory=list)
    model: list[str] = field(default_factory=list)

    def texts(self) -> tuple[str, str]:
        return _tidy(self.user), _tidy(self.model)

    def __bool__(self) -> bool:
        return bool(self.user or self.model)


class LiveCallBridge:
    """One call held by one Live session."""

    def __init__(
        self,
        call_id: str,
        interface_name: str,
        interface_path: str,
        engine_name: str,
        engine: LiveEngineBase,
        sink: LiveSink,
        ports: BridgePorts,
        *,
        on_lost: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.call_id = call_id
        self.interface_name = interface_name
        self.interface_path = interface_path
        self.engine_name = engine_name
        self.history_path = "live_" + interface_path.replace("/", "_")
        self._engine = engine
        self._sink = sink
        self._ports = ports
        self._on_lost = on_lost
        self._turn = _Turn()
        self._turns: list[tuple[str, str]] = []
        self._pump: asyncio.Task[None] | None = None
        self._tools: set[asyncio.Task[None]] = set()
        self._ending = False
        self._errors = 0
        self._speaker_id = ""
        self._speaker_name = ""
        self.started = False
        #: ``time.monotonic()`` until which the queued model audio is still playing.
        self.speaking_until = 0.0
        self.resume_handle: str | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> bool:
        """Open the session. ``False`` (and nothing left open) when it cannot be opened."""
        voice, language = self._ports.voice()
        try:
            await self._engine.open_session(
                self.call_id,
                system_instruction=await self._ports.system_instruction(),
                tools=self._ports.tools(),
                voice=voice,
                language=language,
                resume_handle=self.resume_handle,
            )
        except Exception as exc:  # noqa: BLE001 - a call falls back to the classic path
            log_warning(f"[live_call] {self.call_id}: could not open a session: {exc}")
            return False
        self.started = True
        self._pump = asyncio.create_task(
            self._run(), name=f"live_call_pump_{self.call_id}"
        )
        log_info(
            f"[live_call] {self.call_id} held by {self.engine_name} "
            f"({self.interface_name} {self.interface_path})"
        )
        return True

    async def end(self) -> None:
        """Close the session, keep what was said, and write the diary entry."""
        if self._ending:
            return
        self._ending = True
        pump, self._pump = self._pump, None
        for task in list(self._tools):
            task.cancel()
        try:
            await self._engine.close_session(self.call_id)
        except Exception as exc:  # noqa: BLE001
            log_debug(f"[live_call] {self.call_id}: close failed: {exc}")
        if pump is not None and pump is not asyncio.current_task():
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        await self._finish_turn()
        if self._turns:
            try:
                await self._ports.record_diary(
                    self.interface_name, self.interface_path, list(self._turns)
                )
            except Exception as exc:  # noqa: BLE001
                log_warning(f"[live_call] {self.call_id}: diary entry failed: {exc}")
        log_info(f"[live_call] {self.call_id} ended after {len(self._turns)} turns")

    # --------------------------------------------------------------------- input

    async def feed(
        self, pcm16k: bytes, speaker_id: str = "", speaker_name: str = ""
    ) -> None:
        """What the caller says: mono 16-bit 16 kHz PCM."""
        if not self.started or self._ending or not pcm16k:
            return
        if speaker_id:
            self._speaker_id, self._speaker_name = speaker_id, speaker_name
        try:
            await self._engine.send_audio(self.call_id, pcm16k, 16000)
        except Exception as exc:  # noqa: BLE001 - a lost frame must not end a call
            log_debug(f"[live_call] {self.call_id}: frame dropped: {exc}")

    async def tell(self, text: str, kind: str = "context") -> None:
        """Give the model a line: something it should know, or something to answer."""
        if not self.started or self._ending or not text.strip():
            return
        try:
            await self._engine.send_text(self.call_id, text, kind)
        except Exception as exc:  # noqa: BLE001
            log_debug(f"[live_call] {self.call_id}: text not delivered: {exc}")

    @property
    def is_speaking(self) -> bool:
        return time.monotonic() < self.speaking_until

    # -------------------------------------------------------------------- output

    async def _run(self) -> None:
        try:
            async for event in self._engine.receive_events(self.call_id):
                await self._handle(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_warning(f"[live_call] {self.call_id}: event stream failed: {exc}")
        if not self._ending:
            await self._lost("the Live session ended")

    async def _handle(self, event: LiveEvent) -> None:
        kind = event.type
        if kind is LiveEventType.AUDIO and event.audio:
            rate = int(event.metadata.get("rate") or DEFAULT_OUTPUT_RATE)
            self._errors = 0
            now = time.monotonic()
            self.speaking_until = max(self.speaking_until, now) + len(event.audio) / (
                2 * rate
            )
            try:
                await self._sink.stream(event.audio, rate)
            except Exception as exc:  # noqa: BLE001
                log_debug(f"[live_call] {self.call_id}: audio not played: {exc}")
        elif kind is LiveEventType.TRANSCRIPT and event.text:
            side = (
                self._turn.user
                if event.metadata.get("side") == "input"
                else self._turn.model
            )
            side.append(event.text.strip())
        elif kind is LiveEventType.TURN_COMPLETE:
            await self._finish_turn()
        elif kind is LiveEventType.INTERRUPTED:
            self.speaking_until = 0.0
            try:
                await self._sink.flush()
            except Exception as exc:  # noqa: BLE001
                log_debug(f"[live_call] {self.call_id}: flush failed: {exc}")
        elif kind is LiveEventType.TOOL_CALL and event.tool_call is not None:
            task = asyncio.create_task(self._tool(event.tool_call))
            self._tools.add(task)
            task.add_done_callback(self._tools.discard)
        elif kind is LiveEventType.STATE:
            handle = event.metadata.get("resume_handle")
            if handle:
                self.resume_handle = str(handle)
            log_debug(f"[live_call] {self.call_id}: session {event.detail}")
        elif kind is LiveEventType.ERROR:
            self._errors += 1
            log_warning(f"[live_call] {self.call_id}: engine error: {event.detail}")
            if self._errors >= MAX_ERRORS:
                await self._lost("the Live engine keeps failing")

    async def _tool(self, call: Any) -> None:
        context = {
            "source": "live_voice",
            "interface_path": self.interface_path,
            "history_path": self.history_path,
            "call_id": self.call_id,
            "interface": self.interface_name,
        }
        try:
            result = await asyncio.wait_for(
                self._ports.run_tool(call.name, dict(call.args), context), TOOL_TIMEOUT
            )
        except asyncio.TimeoutError:
            result = {"status": "error", "error": f"{call.name} timed out"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the model is told, the call goes on
            log_warning(f"[live_call] {self.call_id}: tool {call.name} failed: {exc}")
            result = {"status": "error", "error": "the action failed"}
        if self._ending:
            return
        try:
            await self._engine.send_tool_response(
                self.call_id, call.call_id, call.name, result
            )
        except Exception as exc:  # noqa: BLE001
            log_debug(f"[live_call] {self.call_id}: tool result not delivered: {exc}")

    async def _finish_turn(self) -> None:
        turn, self._turn = self._turn, _Turn()
        if not turn:
            return
        user, model = turn.texts()
        self._turns.append((user, model))
        if user:
            await self._save(
                user, self._speaker_name or "[voice_user]", self._speaker_id or None
            )
        if model:
            await self._save(model, "self", "self")
        if user and model:
            try:
                await self._ports.record_memory(user, model)
            except Exception as exc:  # noqa: BLE001
                log_debug(f"[live_call] {self.call_id}: memory not recorded: {exc}")

    async def _save(self, text: str, name: str, sender_id: str | None) -> None:
        try:
            await self._ports.save_message(
                interface_path=self.history_path,
                message_text=text,
                sender_name=name,
                sender_id=sender_id,
            )
        except Exception as exc:  # noqa: BLE001
            log_debug(f"[live_call] {self.call_id}: history not saved: {exc}")

    async def _lost(self, reason: str) -> None:
        log_warning(f"[live_call] {self.call_id}: {reason}")
        if self._on_lost is not None:
            await self._on_lost(self.call_id)


class LiveCalls:
    """The bridges of the calls running now, keyed by call id."""

    def __init__(self, ports: Callable[[], BridgePorts]) -> None:
        self._ports = ports
        self._bridges: dict[str, LiveCallBridge] = {}

    def get(self, call_id: str | None) -> LiveCallBridge | None:
        return self._bridges.get(call_id) if call_id else None

    def by_history_path(self, history_path: str) -> LiveCallBridge | None:
        """The call whose conversation is stored under ``history_path`` (``live_...``)."""
        for bridge in self._bridges.values():
            if bridge.history_path == history_path:
                return bridge
        return None

    async def tell_all(self, text: str, kind: str = "context") -> None:
        """Give every running call a line (what was said in a chat, for instance)."""
        for bridge in self.active():
            await bridge.tell(text, kind)

    def by_path(self, interface_path: str) -> LiveCallBridge | None:
        for bridge in self._bridges.values():
            own = bridge.interface_path
            if interface_path == own or interface_path.startswith(own + "/"):
                return bridge
        return None

    def active(self) -> list[LiveCallBridge]:
        return list(self._bridges.values())

    async def begin(
        self,
        call_id: str,
        interface_name: str,
        interface_path: str,
        sink: LiveSink,
        *,
        on_lost: Callable[[str], Awaitable[None]] | None = None,
    ) -> LiveCallBridge | None:
        """Hand a call to a Live engine. ``None`` leaves it on the classic path."""
        ports = self._ports()
        picked = ports.resolve_engine()
        if picked is None or call_id in self._bridges:
            return None
        name, engine = picked

        async def lost(lost_id: str) -> None:
            self._bridges.pop(lost_id, None)
            if on_lost is not None:
                await on_lost(lost_id)

        bridge = LiveCallBridge(
            call_id,
            interface_name,
            interface_path,
            name,
            engine,
            sink,
            ports,
            on_lost=lost,
        )
        if not await bridge.start():
            return None
        self._bridges[call_id] = bridge
        return bridge

    async def end(self, call_id: str) -> None:
        bridge = self._bridges.pop(call_id, None)
        if bridge is not None:
            await bridge.end()


# --------------------------------------------------------------------- the Core's ports


def _resolve_engine() -> tuple[str, LiveEngineBase] | None:
    from core.cortex_registry import get_cortex_registry
    from core.config_manager import config_registry

    registry = get_cortex_registry()
    engines = registry.get_engines_by_cortex("live")
    wanted = config_registry.get_value("LIVE_CORTEX", "Default")
    name = wanted if wanted in engines else (engines[0] if engines else None)
    if name is None:
        return None
    engine = registry.get_engine(name)
    if not isinstance(engine, LiveEngineBase):
        return None
    return name, engine


def _live_tools() -> list[dict[str, Any]]:
    from core.live_tool_registry import LiveToolRegistry

    tools: list[dict[str, Any]] = []
    for manifest in LiveToolRegistry.build_manifests() or []:
        tools.append(
            {
                "name": manifest.name,
                "description": manifest.description,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "required": p.required,
                        "enum": p.enum,
                        "schema": p.schema,
                    }
                    for p in manifest.parameters
                ],
            }
        )
    return tools


async def _system_instruction() -> str:
    from core.prompt_engine import build_live_system_instruction

    return await build_live_system_instruction()


async def _run_tool(
    name: str, args: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    from core.action_parser import run_action

    # Actions that answer into the conversation (memory search) find the call by this path.
    origin = SimpleNamespace(
        interface_path=context.get("history_path"), chat_id=None, from_user=None
    )
    raw = await run_action({"type": name, "payload": args}, context, None, origin)
    return raw if isinstance(raw, dict) else {"status": "ok"}


async def _save_message(**kwargs: Any) -> Any:
    from core.chat_history_cache import save_chat_message

    return await save_chat_message(**kwargs)


async def _record_memory(user: str, model: str) -> None:
    from core.synth_core_memory import silently_record_memory

    await silently_record_memory(
        user_text=user,
        response_text=model,
        tags='["voice", "auto"]',
        scope="general",
        source="voice",
    )


async def _record_diary(
    interface_name: str, interface_path: str, turns: list[tuple[str, str]]
) -> None:
    from plugins.ai_diary import add_diary_entry

    content = "\n\n".join(m for (_u, m) in turns if m)
    user_msgs = "\n\n".join(u for (u, _m) in turns if u)
    await asyncio.to_thread(
        add_diary_entry,
        content=content,
        user_message=user_msgs,
        interaction_summary=f"Voice session ended - {len(turns)} turns",
        interface=interface_name,
        chat_id=interface_path,
    )


def _voice() -> tuple[str, str]:
    from core.config_manager import config_registry

    return (
        str(config_registry.get_value("LIVE_VOICE_NAME", "") or ""),
        str(config_registry.get_value("LIVE_LANGUAGE", "") or ""),
    )


def core_bridge_ports() -> BridgePorts:
    """The bridge ports backed by the running Core."""
    return BridgePorts(
        resolve_engine=_resolve_engine,
        system_instruction=_system_instruction,
        tools=_live_tools,
        run_tool=_run_tool,
        save_message=_save_message,
        record_memory=_record_memory,
        record_diary=_record_diary,
        voice=_voice,
    )


_calls: LiveCalls | None = None


def get_live_calls() -> LiveCalls:
    """The running Core's calls held by Live engines."""
    global _calls
    if _calls is None:
        _calls = LiveCalls(core_bridge_ports)
    return _calls
