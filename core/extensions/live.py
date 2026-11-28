"""Live engines that live in an extension (capability ``live.provider``).

A Live engine is a full-duplex conversation with a model: sound goes in, sound comes out,
the model may call tools and may be interrupted. It is not Auris plus Vox (that is the
classic call path, which already runs on those engines).

The Core owns the *conversation*: the persona, the tools, the history, who is speaking and
what a call is (``core.live_call_bridge``). The extension owns the *model connection*: it
turns the Core's neutral session into its provider's own wire protocol and back. An
extension that declares ``provides.engines = ["live"]`` and holds ``live.provider``
announces one engine, which then appears next to any other in the Live selector
(``LIVE_CORTEX``) and is driven through the same ``LiveEngineBase`` interface as an
in-tree engine.

Extension to Core:

``live.provider``
    announce the engine: ``name``, ``label``, the PCM rates it takes and gives
    (16 kHz in, 24 kHz out by default) and the longest session it can hold, if any;
``live.event``
    something the model did in a session: ``audio`` (raw 16-bit mono PCM, any chunk of
    up to ~8 s), ``transcript`` (``side`` is ``input`` for the caller, ``output`` for the
    model), ``tool_call``, ``turn_complete``, ``interrupted`` (the caller spoke over the
    model: drop what is queued), ``state`` (``connected``, ``reconnecting``, ``closed``,
    with an optional ``resume_handle``) or ``error``.

Core to extension: ``live.open`` (a session: persona, tools, voice, language, resume
handle), ``live.audio`` (what the caller says, 16 kHz mono PCM; a notification),
``live.text`` (a line for the model: ``kind`` is ``context`` for something it should know
or ``turn`` for something it should answer), ``live.tool_result`` and ``live.close``.

The tool declarations the Core sends are neutral (name, description, typed parameters);
turning them into a provider's function declarations is the extension's job.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import AsyncIterator
from typing import Any, Protocol

from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.gateway import ApiMethod, CallContext, Gateway, Param, Session
from plugins.live_base import (
    LiveEngineBase,
    LiveEvent,
    LiveEventType,
    ToolCallPayload,
)

CAP_LIVE = "live.provider"
ENGINE_KIND = "live"

#: One audio event: about 8 s of 24 kHz mono (a protocol message is 1 MiB, base64 grows it
#: by a third).
MAX_AUDIO_BYTES = 400_000
MAX_TEXT = 20_000
MAX_RESULT_BYTES = 60_000
OPEN_TIMEOUT = 30.0
CALL_TIMEOUT = 15.0
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
EVENT_TYPES = (
    "audio",
    "transcript",
    "tool_call",
    "turn_complete",
    "interrupted",
    "state",
    "error",
)
STATES = ("connected", "reconnecting", "closed")
_RATES = (8000, 16000, 24000, 32000, 44100, 48000)
_CLOSED = object()


class LiveHost(Protocol):
    """Where a remote engine is offered to the Core."""

    def taken(self, name: str) -> bool:
        """An engine of this name exists and is **not** a remote one."""
        ...

    def register(self, name: str, engine: LiveEngineBase, label: str) -> None: ...

    def unregister(self, name: str) -> None: ...


class RemoteLiveEngine(LiveEngineBase):
    """A Live engine whose model connection runs in an extension."""

    def __init__(
        self,
        session: Session,
        name: str,
        label: str,
        *,
        input_rate: int = 16000,
        output_rate: int = 24000,
        max_session_seconds: int = 0,
    ) -> None:
        self.name = name
        self.display_name = label or name
        self.input_rate = input_rate
        self.output_rate = output_rate
        #: Read by ``LiveSessionManager`` to arm the rejoin timer; ``None`` for no limit.
        self.MAX_SESSION_SECONDS: int | None = max_session_seconds or None
        self._session = session
        self._queues: dict[str, asyncio.Queue[Any]] = {}

    @property
    def supports_input(self) -> bool:
        return True

    @property
    def supports_output(self) -> bool:
        return True

    @property
    def alive(self) -> bool:
        return not self._session.peer.closed

    def has_session(self, session_id: str) -> bool:
        return session_id in self._queues

    # ------------------------------------------------------------ Core to extension

    async def open_session(self, session_id: str, **kwargs: Any) -> None:
        if not self.alive:
            raise RuntimeError(f"the extension providing {self.name!r} is not running")
        if session_id in self._queues:
            return
        # The queue exists before the request: events may arrive before it returns.
        self._queues[session_id] = asyncio.Queue()
        params: dict[str, Any] = {
            "session_id": session_id,
            "system_instruction": str(kwargs.get("system_instruction") or ""),
            "tools": list(kwargs.get("tools") or []),
            "voice": str(kwargs.get("voice") or ""),
            "language": str(kwargs.get("language") or ""),
            "resume_handle": kwargs.get("resume_handle"),
            "options": dict(kwargs.get("options") or {}),
        }
        try:
            await self._session.call("live.open", params, timeout=OPEN_TIMEOUT)
        except RpcError as exc:
            self._queues.pop(session_id, None)
            raise RuntimeError(f"{self.name}: could not open a session: {exc}") from exc

    async def close_session(self, session_id: str) -> None:
        queue = self._queues.pop(session_id, None)
        if queue is None:
            return
        queue.put_nowait(_CLOSED)
        if self.alive:
            try:
                await self._session.call(
                    "live.close", {"session_id": session_id}, timeout=CALL_TIMEOUT
                )
            except RpcError:
                return

    async def send_audio(
        self, session_id: str, chunk: bytes, sample_rate: int = 16000
    ) -> None:
        if session_id not in self._queues or not self.alive or not chunk:
            return
        for start in range(0, len(chunk), MAX_AUDIO_BYTES):
            part = chunk[start : start + MAX_AUDIO_BYTES]
            try:
                await self._session.notify(
                    "live.audio",
                    {
                        "session_id": session_id,
                        "pcm": base64.b64encode(part).decode("ascii"),
                        "rate": sample_rate,
                    },
                )
            except RpcError:
                return

    async def send_text(
        self, session_id: str, text: str, kind: str = "context"
    ) -> None:
        if session_id not in self._queues or not self.alive:
            return
        try:
            await self._session.call(
                "live.text",
                {
                    "session_id": session_id,
                    "text": text[:MAX_TEXT],
                    "kind": kind if kind in ("context", "turn") else "context",
                },
                timeout=CALL_TIMEOUT,
            )
        except RpcError:
            return

    async def send_tool_response(
        self, session_id: str, call_id: str, name: str, result: dict[str, Any]
    ) -> None:
        if session_id not in self._queues or not self.alive:
            return
        try:
            text = json.dumps(result, default=str)
        except (TypeError, ValueError):
            text = json.dumps({"status": "error", "error": "unserialisable result"})
        if len(text) > MAX_RESULT_BYTES:
            text = json.dumps(
                {
                    "status": "ok",
                    "truncated": True,
                    "text": text[: MAX_RESULT_BYTES // 2],
                }
            )
        try:
            await self._session.call(
                "live.tool_result",
                {
                    "session_id": session_id,
                    "call_id": call_id,
                    "name": name,
                    "result": json.loads(text),
                },
                timeout=CALL_TIMEOUT,
            )
        except RpcError:
            return

    # ------------------------------------------------------------ extension to Core

    async def receive_events(self, session_id: str) -> AsyncIterator[LiveEvent]:
        queue = self._queues.get(session_id)
        if queue is None:
            return
        while True:
            item = await queue.get()
            if item is _CLOSED:
                return
            yield item

    def deliver(self, session_id: str, event: LiveEvent) -> bool:
        queue = self._queues.get(session_id)
        if queue is None:
            return False
        queue.put_nowait(event)
        return True

    def shutdown(self) -> None:
        """The extension is gone: tell every open session, then end its stream."""
        for queue in list(self._queues.values()):
            queue.put_nowait(
                LiveEvent(type=LiveEventType.STATE, detail="closed", is_final=True)
            )
            queue.put_nowait(_CLOSED)
        self._queues.clear()


def _event_from(args: dict[str, Any]) -> LiveEvent:
    kind = args["type"]
    if kind == "audio":
        try:
            raw = base64.b64decode(args["pcm"], validate=True)
        except ValueError:
            raise RpcError(ErrorCode.INVALID_PARAMS, "pcm must be base64") from None
        if not raw or len(raw) > MAX_AUDIO_BYTES or len(raw) % 2:
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "pcm must be whole 16-bit samples, not empty"
            )
        return LiveEvent(
            type=LiveEventType.AUDIO, audio=raw, metadata={"rate": args["rate"]}
        )
    if kind == "transcript":
        text = args["text"].strip()
        if not text:
            raise RpcError(ErrorCode.INVALID_PARAMS, "a transcript needs text")
        return LiveEvent(
            type=LiveEventType.TRANSCRIPT,
            text=text,
            is_final=bool(args["final"]),
            metadata={"side": args["side"]},
        )
    if kind == "tool_call":
        if not args["name"] or not args["call_id"]:
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "a tool call needs call_id and name"
            )
        tool_args = args["args"] if isinstance(args["args"], dict) else {}
        return LiveEvent(
            type=LiveEventType.TOOL_CALL,
            tool_call=ToolCallPayload(args["call_id"], args["name"], tool_args),
        )
    if kind == "turn_complete":
        return LiveEvent(type=LiveEventType.TURN_COMPLETE, is_final=True)
    if kind == "interrupted":
        return LiveEvent(type=LiveEventType.INTERRUPTED)
    if kind == "state":
        if args["state"] not in STATES:
            raise RpcError(
                ErrorCode.INVALID_PARAMS, f"state must be one of {', '.join(STATES)}"
            )
        meta = {"resume_handle": args["resume_handle"]} if args["resume_handle"] else {}
        return LiveEvent(type=LiveEventType.STATE, detail=args["state"], metadata=meta)
    return LiveEvent(type=LiveEventType.ERROR, detail=args["detail"][:500] or "error")


def register_live_api(gateway: Gateway, host: LiveHost) -> None:
    """Register ``live.*`` on the gateway."""

    def engine_of(ctx: CallContext) -> RemoteLiveEngine:
        engine = ctx.session.state.get("live")
        if engine is None:
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "announce the engine with live.provider first"
            )
        return engine

    async def provider(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        if ENGINE_KIND not in ctx.manifest.provides.engines:
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                'declare provides.engines = ["live"] in the manifest first',
            )
        name = args["name"]
        if not NAME_RE.match(name):
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "name must be a lowercase identifier"
            )
        if host.taken(name):
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"{name!r} is a built-in engine and cannot be replaced",
            )
        old = ctx.session.state.pop("live", None)
        if old is not None:
            host.unregister(old.name)
            old.shutdown()
        engine = RemoteLiveEngine(
            ctx.session,
            name,
            args["label"],
            input_rate=args["input_rate"],
            output_rate=args["output_rate"],
            max_session_seconds=args["max_session_seconds"],
        )
        ctx.session.state["live"] = engine
        host.register(name, engine, args["label"] or name)
        return {"ok": True}

    async def event(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        engine = engine_of(ctx)
        live_event = _event_from(args)
        # A late event of a session the Core already closed is not an error.
        return {"accepted": engine.deliver(args["session_id"], live_event)}

    for method in (
        ApiMethod(
            "live.provider",
            CAP_LIVE,
            (
                Param("name", max_length=32),
                Param("label", max_length=120, required=False, default=""),
                Param(
                    "input_rate",
                    "int",
                    required=False,
                    default=16000,
                    choices=_RATES,
                ),
                Param(
                    "output_rate",
                    "int",
                    required=False,
                    default=24000,
                    choices=_RATES,
                ),
                Param(
                    "max_session_seconds",
                    "int",
                    required=False,
                    default=0,
                    minimum=0,
                    maximum=86_400,
                ),
            ),
            provider,
            "Announce the Live engine this extension provides.",
        ),
        ApiMethod(
            "live.event",
            CAP_LIVE,
            (
                Param("session_id", max_length=64),
                Param("type", choices=EVENT_TYPES),
                Param(
                    "pcm",
                    max_length=MAX_AUDIO_BYTES * 4 // 3 + 8,
                    required=False,
                    default="",
                ),
                Param("rate", "int", required=False, default=24000, choices=_RATES),
                Param("text", max_length=MAX_TEXT, required=False, default=""),
                Param(
                    "side",
                    required=False,
                    default="output",
                    choices=("input", "output"),
                ),
                Param("final", "bool", required=False, default=True),
                Param("call_id", max_length=128, required=False, default=""),
                Param("name", max_length=128, required=False, default=""),
                Param("args", "object", required=False, default=None),
                Param("state", max_length=32, required=False, default=""),
                Param("resume_handle", max_length=4000, required=False, default=""),
                Param("detail", max_length=500, required=False, default=""),
            ),
            event,
            "Something the model did in a session.",
        ),
    ):
        gateway.register(method)

    def on_attach(session: Session) -> None:
        async def cleanup() -> None:
            engine = session.state.pop("live", None)
            if engine is not None:
                host.unregister(engine.name)
                engine.shutdown()

        session.on_close(cleanup)

    gateway.on_attach.append(on_attach)


class CoreLiveHost:
    """``LiveHost`` backed by the Core's Live and cortex registries."""

    @staticmethod
    def _live() -> Any:
        from core.live_registry import LIVE_REGISTRY

        return LIVE_REGISTRY

    @staticmethod
    def _cortex() -> Any:
        from core.cortex_registry import get_cortex_registry

        return get_cortex_registry()

    def taken(self, name: str) -> bool:
        live = self._live()
        if name in live.get_available_engines():
            return not isinstance(live.get_instance(name), RemoteLiveEngine)
        cortex = self._cortex()
        if name in cortex.get_available_engines():
            return not isinstance(cortex.get_engine(name), RemoteLiveEngine)
        return False

    def register(self, name: str, engine: LiveEngineBase, label: str) -> None:
        self._live().register_instance(name, engine, label=label)
        # The Live selector (``LIVE_CORTEX``) lists the cortex registry's "live" kind.
        self._cortex().register_instance(name, engine, cortex=ENGINE_KIND, label=label)

    def unregister(self, name: str) -> None:
        self._live().unregister_engine(name)
        self._cortex().unregister_engine(name)
