"""Karada for extensions: what the avatar is doing, as frames (capabilities ``karada.subscribe``
and ``karada.presence``).

The Karada state server is the single source of truth for "what the avatar is doing": it tells
every registered *transport* when she starts thinking, writing or talking, when a clip of her
voice plays, what her face looks like. The WebUI is one transport, the Karada API socket another.
An extension that draws or carries the avatar somewhere else (a game room, a headset, a renderer)
is a third, through this API.

``karada.subscribe``
    the extension becomes a transport. From then on every frame the server broadcasts reaches it
    as a ``karada.frame`` notification ``{"kind", "payload"}``, kind one of ``animation``,
    ``audio``, ``face``, ``model``, ``expression``, ``presence`` or ``direct``, the payload exactly
    Karada's own (nothing is rewritten, the extension maps it to its world). The answer carries the
    state to catch up with (``{"animation", "presence"}``). Audio travels as the URL Karada already
    serves, never as bytes.

``karada.unsubscribe``
    stop receiving frames (and a session that ends is unsubscribed for it).

``karada.state``
    the same catch-up state, at any time.

``karada.presence``
    tell Karada she is arriving in (``"summon"``) or leaving (``None``) the world this extension
    carries, so every client knows. An extension can only clear a presence it set itself.

Frames are best-effort and bounded: a slow or gone extension is skipped, not waited for.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.gateway import ApiMethod, CallContext, Gateway, Param, Session

CAP_SUBSCRIBE = "karada.subscribe"
CAP_PRESENCE = "karada.presence"
FRAME_KINDS = (
    "animation",
    "audio",
    "face",
    "model",
    "expression",
    "presence",
    "direct",
)
#: Frames that may be waiting for one extension; past this the oldest are dropped.
MAX_PENDING = 64
PRESENCE_STATES = ("summon",)


class KaradaHost(Protocol):
    """What the Core offers: the state server, reduced to what an extension may use."""

    def add_transport(self, transport: Any) -> None: ...

    def remove_transport(self, transport: Any) -> None: ...

    def snapshot(self) -> dict[str, Any]:
        """``{"animation": {...}, "presence": str | None}``."""
        ...

    def presence(self) -> str | None: ...

    async def set_presence(self, name: str | None, source: str) -> None: ...


def transport_class() -> type:
    """The transport. Built here so importing this module needs no Karada code."""
    from core.karada_transport import KaradaTransport

    class ExtensionTransport(KaradaTransport):
        """Hands each Karada broadcast to one extension as a ``karada.frame``."""

        def __init__(self, session: Session) -> None:
            self._session = session
            self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
                MAX_PENDING
            )
            self._pump: asyncio.Task[None] | None = None
            self.sent = 0
            self.dropped = 0

        def start(self) -> None:
            if self._pump is None:
                self._pump = asyncio.get_running_loop().create_task(self._run())

        def stop(self) -> None:
            if self._pump is not None:
                self._pump.cancel()
                self._pump = None

        async def _run(self) -> None:
            while True:
                kind, payload = await self._queue.get()
                if self._session.peer.closed:
                    return
                try:
                    await self._session.notify(
                        "karada.frame", {"kind": kind, "payload": payload}
                    )
                    self.sent += 1
                except RpcError:
                    self.dropped += 1

        async def _put(self, kind: str, payload: dict[str, Any]) -> None:
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            self._queue.put_nowait((kind, dict(payload or {})))

        async def broadcast_animation(self, payload: dict[str, Any]) -> None:
            await self._put("animation", payload)

        async def broadcast_audio(self, payload: dict[str, Any]) -> None:
            await self._put("audio", payload)

        async def broadcast_face(self, payload: dict[str, Any]) -> None:
            await self._put("face", payload)

        async def broadcast_model(self, payload: dict[str, Any]) -> None:
            await self._put("model", payload)

        async def broadcast_expression(self, payload: dict[str, Any]) -> None:
            await self._put("expression", payload)

        async def broadcast_presence(self, payload: dict[str, Any]) -> None:
            await self._put("presence", payload)

        async def send_to_session(
            self, session_id: str, payload: dict[str, Any]
        ) -> None:
            await self._put("direct", payload)

        async def preload_asset(
            self, session_id: str | None, payload: dict[str, Any]
        ) -> None:
            return None  # an extension fetches an asset when it first needs it

        def get_connected_sessions(self) -> list[str]:
            return [self._session.extension_id] if not self._session.peer.closed else []

    return ExtensionTransport


def _slot(session: Session) -> dict[str, Any]:
    return session.state.setdefault("karada", {"transport": None, "presence": None})


def register_karada_api(gateway: Gateway, host: KaradaHost) -> None:
    """Register the Karada methods and undo what an extension set up when it goes."""
    cls = transport_class()

    def release(session: Session) -> None:
        slot = _slot(session)
        transport = slot["transport"]
        if transport is not None:
            host.remove_transport(transport)
            transport.stop()
            slot["transport"] = None

    async def clear_presence(session: Session) -> None:
        slot = _slot(session)
        if slot["presence"] is not None and host.presence() == slot["presence"]:
            await host.set_presence(None, session.extension_id)
        slot["presence"] = None

    def on_attach(session: Session) -> None:
        async def cleanup() -> None:
            release(session)
            await clear_presence(session)

        session.on_close(cleanup)

    gateway.on_attach.append(on_attach)

    async def subscribe(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        slot = _slot(ctx.session)
        if slot["transport"] is None:
            transport = cls(ctx.session)
            transport.start()
            host.add_transport(transport)
            slot["transport"] = transport
        return host.snapshot()

    async def unsubscribe(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        was = _slot(ctx.session)["transport"] is not None
        release(ctx.session)
        return {"unsubscribed": was}

    async def state(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        return host.snapshot()

    async def presence(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        slot = _slot(ctx.session)
        wanted = args["state"]
        if wanted is None or wanted == "":
            if slot["presence"] is None:
                raise RpcError(
                    ErrorCode.PERMISSION_DENIED,
                    "you can only clear a presence you set yourself",
                )
            await clear_presence(ctx.session)
            return {"presence": host.presence()}
        if wanted not in PRESENCE_STATES:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"presence is one of {', '.join(PRESENCE_STATES)} or null",
            )
        await host.set_presence(wanted, ctx.extension_id)
        slot["presence"] = wanted
        return {"presence": host.presence()}

    gateway.register(
        ApiMethod(
            "karada.subscribe",
            CAP_SUBSCRIBE,
            (),
            subscribe,
            "Receive what the avatar does as karada.frame notifications.",
        )
    )
    gateway.register(
        ApiMethod(
            "karada.unsubscribe",
            CAP_SUBSCRIBE,
            (),
            unsubscribe,
            "Stop receiving frames.",
        )
    )
    gateway.register(
        ApiMethod("karada.state", CAP_SUBSCRIBE, (), state, "What the avatar does now.")
    )
    gateway.register(
        ApiMethod(
            "karada.presence",
            CAP_PRESENCE,
            (Param("state", "json", required=False, default=None),),
            presence,
            "Say she is arriving in a world (summon) or leaving it (null).",
        )
    )


class CoreKaradaHost:
    """``KaradaHost`` backed by the Core's Karada state server."""

    @staticmethod
    def _server() -> Any:
        from core.animation_handler import get_karada_state_server

        return get_karada_state_server()

    def add_transport(self, transport: Any) -> None:
        self._server().add_transport(transport)

    def remove_transport(self, transport: Any) -> None:
        self._server().remove_transport(transport)

    def snapshot(self) -> dict[str, Any]:
        server = self._server()
        return {
            "animation": server.get_current_animation_state(),
            "presence": server.get_presence(),
        }

    def presence(self) -> str | None:
        return self._server().get_presence()

    async def set_presence(self, name: str | None, source: str) -> None:
        await self._server().set_presence(name, source)
