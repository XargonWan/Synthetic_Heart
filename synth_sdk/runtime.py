"""The extension side of the connection: :class:`Context` and the run loop.

An extension's entry point is ``async def main(ctx: Context)``. It runs until it
returns or until the Core asks it to stop (``ctx.stopping`` is set; the task is
cancelled after the grace period).

Everything an extension can do goes through ``ctx``: each call is an RPC request
that the Core checks against the permissions the extension declared and the user
granted. Nothing here imports ``core``. Standard library only.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import secrets
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from synth_sdk import __version__
from synth_sdk.rpc import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ErrorCode,
    RpcError,
    RpcPeer,
)

EventCallback = Callable[[str, Any], Awaitable[None] | None]
DEFAULT_STOP_GRACE = 5.0


class PermissionDenied(RpcError):
    """The Core refused the call: the capability is not declared or not granted."""


def _raise_typed(error: RpcError) -> RpcError:
    if error.code == ErrorCode.PERMISSION_DENIED:
        return PermissionDenied(error.code, error.message, error.data)
    return error


class _Namespace:
    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx


class Log(_Namespace):
    """Log lines go to the Core log, tagged with this extension's id."""

    def _write(self, level: str, message: str) -> None:
        self._ctx.fire("log.write", {"level": level, "message": str(message)[:4000]})

    def debug(self, message: str) -> None:
        self._write("debug", message)

    def info(self, message: str) -> None:
        self._write("info", message)

    def warning(self, message: str) -> None:
        self._write("warning", message)

    def error(self, message: str) -> None:
        self._write("error", message)


class Config(_Namespace):
    """The extension's own declared configuration keys."""

    async def get(self, key: str) -> Any:
        return (await self._ctx.call("config.get", key=key))["value"]

    async def all(self) -> dict[str, Any]:
        return (await self._ctx.call("config.all"))["values"]


class Storage(_Namespace):
    """Key/value storage private to this extension (JSON values)."""

    async def get(self, key: str, default: Any = None) -> Any:
        result = await self._ctx.call("storage.get", key=key)
        return result["value"] if result["found"] else default

    async def set(self, key: str, value: Any) -> None:
        await self._ctx.call("storage.set", key=key, value=value)

    async def delete(self, key: str) -> bool:
        return bool((await self._ctx.call("storage.delete", key=key))["deleted"])

    async def keys(self, prefix: str = "", limit: int = 1000) -> list[str]:
        return list(
            (await self._ctx.call("storage.keys", prefix=prefix, limit=limit))["keys"]
        )


class Stats(_Namespace):
    """Statistics. Series names are prefixed with the extension id by the Core."""

    def incr(self, name: str, n: int = 1, **labels: str) -> None:
        self._ctx.fire("stats.incr", {"name": name, "n": n, "labels": labels})

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        self._ctx.fire(
            "stats.observe", {"name": name, "seconds": seconds, "labels": labels}
        )

    def gauge(self, name: str, value: float) -> None:
        self._ctx.fire("stats.gauge", {"name": name, "value": value})


class Events(_Namespace):
    """Publish and subscribe to topics."""

    async def publish(self, topic: str, payload: Any = None) -> int:
        params: dict[str, Any] = {"topic": topic}
        if payload is not None:
            params["payload"] = payload
        return int((await self._ctx.call("events.publish", **params))["delivered"])

    async def subscribe(self, topic: str, callback: EventCallback) -> None:
        await self._ctx.call("events.subscribe", topic=topic)
        self._ctx._subscriptions.setdefault(topic, []).append(callback)  # noqa: SLF001

    async def unsubscribe(self, topic: str) -> None:
        await self._ctx.call("events.unsubscribe", topic=topic)
        self._ctx._subscriptions.pop(topic, None)  # noqa: SLF001


class Interfaces(_Namespace):
    """Chat interfaces: announce one, deliver incoming messages, send replies.

    An interface is a connection to a chat platform. Declare it in the manifest
    (``[[provides.interfaces]]`` with its ``paths``), call :meth:`register`, feed
    messages in with :meth:`inbound` and implement :meth:`on_send_message` to
    deliver the Core's replies. The Core owns the chat history, the command
    handling and the message queue: you only report what happened on the platform.
    """

    async def register(
        self,
        interface_id: str,
        *,
        display_name: str,
        bot_id: str | None = None,
        bot_username: str | None = None,
        path_hint: str = "",
        description: str = "",
        resolve_names: bool = False,
        capabilities: list[str] | None = None,
        actions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "interface_id": interface_id,
            "display_name": display_name,
            "path_hint": path_hint,
            "description": description,
            "resolve_names": resolve_names,
        }
        if capabilities is not None:
            # "send_message" is implied. "send_file", "send_audio" and "send_voice"
            # ask the Core to hand you attachments in replies (see on_send_message);
            # "activity" asks for "typing..." signals (see on_activity).
            params["capabilities"] = capabilities
        if actions:
            # Extra actions the model may use on this interface. Each name must be
            # declared in the manifest (``actions = [...]``); the definition is
            # ``{"description", "fields": {name: {"type": "string|integer|boolean",
            # "description", "example", "optional"}}, "notes": [...]}``. Handle them
            # with :meth:`on_action`.
            params["actions"] = actions
        if bot_id is not None:
            params["bot_id"] = str(bot_id)
        if bot_username is not None:
            params["bot_username"] = bot_username
        return dict(await self._ctx.call("interfaces.register", **params))

    async def set_state(
        self, interface_id: str, enabled: bool, reason: str | None = None
    ) -> None:
        """Say whether the interface can deliver right now (for example, a user
        account that is not logged in is registered but disabled, so its setup panel
        can be shown). A disabled interface fails sends; ``reason`` is shown."""
        params: dict[str, Any] = {"interface_id": interface_id, "enabled": enabled}
        if reason:
            params["reason"] = reason
        await self._ctx.call("interfaces.set_state", **params)

    async def unregister(self, interface_id: str) -> bool:
        result = await self._ctx.call(
            "interfaces.unregister", interface_id=interface_id
        )
        return bool(result["unregistered"])

    async def inbound(
        self,
        interface_id: str,
        path: str,
        *,
        text: str,
        sender_id: str,
        sender_name: str,
        sender_full_name: str | None = None,
        message_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        chat_type: str = "private",
        chat_title: str | None = None,
        sender_username: str | None = None,
        chat_username: str | None = None,
        reply_to: dict[str, Any] | None = None,
        skip_mention_check: bool = False,
        is_explicit_trigger: bool = False,
        is_voice_input: bool = False,
        timestamp: str | None = None,
        backlog: bool = False,
        wait_reply: bool = False,
        history: list[dict[str, str]] | None = None,
        reply_timeout: float = 0.0,
        human_count: int | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Deliver a message received on the platform. Returns ``{"queued": True}``
        or ``{"handled": "command"}``.

        ``attachments`` are files you saved under your own data directory (use
        :meth:`Context.inbox_path`), each ``{"path", "filename", "mime_type"}``. The
        Core checks that every path is really inside your data directory, copies the
        file, and refuses the whole message otherwise. The files are yours to delete
        as soon as this call returns."""
        params: dict[str, Any] = {
            "interface_id": interface_id,
            "path": path,
            "text": text,
            "sender_id": str(sender_id),
            "sender_name": sender_name,
            "chat_type": chat_type,
            "skip_mention_check": skip_mention_check,
            "is_explicit_trigger": is_explicit_trigger,
            "is_voice_input": is_voice_input,
            "backlog": backlog,
            "wait_reply": wait_reply,
            "reply_timeout": reply_timeout,
        }
        for key, value in (
            ("sender_full_name", sender_full_name),
            ("message_id", None if message_id is None else str(message_id)),
            ("chat_id", None if chat_id is None else str(chat_id)),
            ("thread_id", None if thread_id is None else str(thread_id)),
            ("timestamp", timestamp),
            ("history", history),
            ("chat_title", chat_title),
            ("sender_username", sender_username),
            ("chat_username", chat_username),
            ("reply_to", reply_to),
            ("human_count", human_count),
            ("attachments", attachments),
        ):
            if value is not None:
                params[key] = value
        # A turn can take minutes: wait as long as the Core is allowed to hold the call.
        patient = (reply_timeout or 900.0) + 60.0 if wait_reply else None
        return dict(await self._ctx.call("interfaces.inbound", patient, **params))

    def on_send_message(
        self,
        handler: Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[Any] | Any],
    ) -> None:
        """Deliver a reply. ``handler(interface_id, payload, origin)`` returns
        ``True``/``False`` or ``{"ok": bool}``. ``payload`` has ``text``,
        ``interface_path`` and optionally ``reply_to``; ``origin`` describes the
        message being answered (``message_id``, ``interface_path``).

        When you registered with ``send_file``/``send_audio``/``send_voice``,
        ``payload["attachments"]`` may hold files to send, each
        ``{"path", "name", "mime_type", "kind", "size"}`` (``kind`` is ``image``,
        ``video``, ``audio`` or ``document``), and ``payload["send_as_voice"]`` asks
        for audio to go as a voice note. The files are copies **readable by you
        only and deleted when this handler returns**: send them before returning."""

        async def adapter(params: dict[str, Any]) -> Any:
            result: Any = handler(
                str(params.get("interface_id", "")),
                dict(params.get("payload") or {}),
                dict(params.get("origin") or {}),
            )
            if asyncio.iscoroutine(result):
                result = await result
            ok = (
                cast(dict[str, Any], result).get("ok")
                if isinstance(result, dict)
                else result
            )
            return {"ok": bool(ok)}

        self._ctx.handle("interface.send_message", adapter)

    async def seen(
        self, interface_id: str, path: str, sender_id: str, timestamp: str
    ) -> bool:
        """Whether the Core already has the message from ``sender_id`` sent at
        ``timestamp`` (ISO-8601) under ``path``. For catching up after downtime."""
        result = await self._ctx.call(
            "interfaces.seen",
            interface_id=interface_id,
            path=path,
            sender_id=str(sender_id),
            timestamp=timestamp,
        )
        return bool(result["seen"])

    async def trainer(self, interface_id: str) -> str | None:
        """The trainer id the operator configured for this interface, if any."""
        result = await self._ctx.call("interfaces.trainer", interface_id=interface_id)
        value = result["trainer_id"]
        return None if value is None else str(value)

    def on_action(
        self, handler: Callable[[str, str, dict[str, Any]], Awaitable[Any] | Any]
    ) -> None:
        """Run one of the interface's own actions. ``handler(interface_id, name,
        payload)`` returns ``{"status": "success"|"failed", "message": str}``."""

        async def adapter(params: dict[str, Any]) -> Any:
            result = handler(
                str(params.get("interface_id", "")),
                str(params.get("name", "")),
                dict(params.get("payload") or {}),
            )
            if asyncio.iscoroutine(result):
                result = await result
            return result if isinstance(result, dict) else {"status": "failed"}

        self._ctx.handle("interface.action", adapter)

    def on_reaction(
        self, handler: Callable[[str, str, str, str], Awaitable[Any] | Any]
    ) -> None:
        """React to a message. ``handler(interface_id, chat_id, message_id, emoji)``
        returns ``True``/``False``. Register with the ``reaction`` capability."""

        async def adapter(params: dict[str, Any]) -> Any:
            result = handler(
                str(params.get("interface_id", "")),
                str(params.get("chat_id") or ""),
                str(params.get("message_id") or ""),
                str(params.get("emoji", "")),
            )
            if asyncio.iscoroutine(result):
                result = await result
            return {"ok": bool(result)}

        self._ctx.handle("interface.reaction", adapter)

    def on_set_avatar(
        self,
        handler: Callable[[str, bytes | None, str | None, bool], Awaitable[Any] | Any],
    ) -> None:
        """Apply the synth avatar. ``handler(interface_id, png_or_None, version,
        force)`` returns ``True`` when something was applied; ``None`` bytes mean the
        avatar was removed. Register with the ``avatar`` capability."""

        async def adapter(params: dict[str, Any]) -> Any:
            raw = params.get("image")
            image = base64.b64decode(raw) if isinstance(raw, str) else None
            result = handler(
                str(params.get("interface_id", "")),
                image,
                params.get("version"),
                bool(params.get("force")),
            )
            if asyncio.iscoroutine(result):
                result = await result
            return {"ok": bool(result)}

        self._ctx.handle("interface.set_avatar", adapter)

    def on_call_control(
        self,
        start: Callable[[str, str], Awaitable[Any] | Any],
        end: Callable[[str, str], Awaitable[Any] | Any],
    ) -> None:
        """Dial out and hang up. ``start(interface_id, user_id)`` and ``end(...)``
        return ``{"status": "success"|"failed", "message": str}``. Register with the
        ``call`` capability."""

        def wrap(
            handler: Callable[[str, str], Awaitable[Any] | Any],
        ) -> Callable[[dict[str, Any]], Awaitable[Any]]:
            async def adapter(params: dict[str, Any]) -> Any:
                result = handler(
                    str(params.get("interface_id", "")),
                    str(params.get("user_id") or ""),
                )
                if asyncio.iscoroutine(result):
                    result = await result
                return result if isinstance(result, dict) else {"status": "failed"}

            return adapter

        self._ctx.handle("interface.start_call", wrap(start))
        self._ctx.handle("interface.end_call", wrap(end))

    def on_activity(
        self,
        handler: Callable[[str, str, str], Awaitable[Any] | Any],
    ) -> None:
        """Show that SyntH is busy. ``handler(interface_id, path, kind)`` is called
        with ``kind`` one of ``typing``, ``record_audio``, ``upload_audio`` or
        ``upload_file``. Register the interface with the ``activity`` capability
        to receive these. Failures are ignored by the Core."""

        async def adapter(params: dict[str, Any]) -> Any:
            result = handler(
                str(params.get("interface_id", "")),
                str(params.get("interface_path", "")),
                str(params.get("kind", "")),
            )
            if asyncio.iscoroutine(result):
                await result
            return {"ok": True}

        self._ctx.handle("interface.activity", adapter)

    def on_resolve_names(
        self,
        handler: Callable[[str, str | None, str | None], Awaitable[Any] | Any],
    ) -> None:
        """Provide live names. ``handler(interface_id, chat_id, thread_id)`` returns
        ``{"chat_name": ..., "message_thread_name": ...}``. Register the interface with
        ``resolve_names=True`` to have it called."""

        async def adapter(params: dict[str, Any]) -> Any:
            result = handler(
                str(params.get("interface_id", "")),
                params.get("chat_id"),
                params.get("thread_id"),
            )
            if asyncio.iscoroutine(result):
                result = await result
            return result if isinstance(result, dict) else {}

        self._ctx.handle("interface.resolve_names", adapter)


class CoreInfo(_Namespace):
    """Facts the Core shares about itself (capability ``core.info``)."""

    async def get(self) -> dict[str, Any]:
        """``{"synth_name", "capabilities": {name: True}, "in_container"}``."""
        return dict(await self._ctx.call("core.info"))


class Avatar(_Namespace):
    """The synth avatar. It belongs to the Core; any extension may read it and stay in
    sync with it (capability ``avatar``; following changes also needs ``events``)."""

    async def get(self) -> dict[str, Any] | None:
        """``{"version", "mime", "image"}`` (``image`` is the PNG in base64), or
        ``None`` when none is set."""
        result = dict(await self._ctx.call("avatar.get"))
        return result if result.get("available") else None

    async def sync(
        self, handler: Callable[[bytes | None, str | None], Awaitable[Any] | Any]
    ) -> None:
        """Keep a copy in sync. ``handler(png_or_None, version)`` is called now with the
        current avatar and again every time it changes; ``None`` bytes mean it was
        removed. Subscribing needs the ``events`` capability."""

        async def apply() -> None:
            current = await self.get()
            image = base64.b64decode(current["image"]) if current else None
            result = handler(image, current["version"] if current else None)
            if asyncio.iscoroutine(result):
                await result

        async def on_event(_topic: str, _payload: Any) -> None:
            await apply()

        await self._ctx.events.subscribe("core.avatar.changed", on_event)
        await apply()


class Calls(_Namespace):
    """Carry the sound and the picture of calls; the Core runs the call itself.

    Register the interface with the ``call`` capability, then :meth:`provider`. A call
    that begins on the platform is :meth:`open`; what the caller says goes in with
    :meth:`audio`; the Core asks you to play its replies with :meth:`on_play`.
    """

    async def provider(self, interface_id: str, video: bool = False) -> None:
        """Announce that ``interface_id`` hosts calls (``video``: it can carry the
        SyntH's face)."""
        await self._ctx.call("calls.provider", interface_id=interface_id, video=video)

    async def open(
        self,
        path: str,
        user_id: str,
        *,
        kind: str = "audio",
        languages: list[str] | None = None,
        announce: bool = True,
    ) -> dict[str, Any]:
        """A call began. Returns ``{"call_id", "kind", "degraded", "video"}`` where
        ``video`` is ``{"fps", "width", "height"}`` when the face can be shown. With
        ``announce`` the answering tone arrives as an :meth:`on_play` request *before*
        this returns, so keep the play handler free to run while you wait."""
        params: dict[str, Any] = {
            "path": path,
            "user_id": str(user_id),
            "kind": kind,
            "announce": announce,
        }
        if languages:
            params["languages"] = languages
        return dict(await self._ctx.call("calls.open", **params))

    async def audio(
        self,
        call_id: str,
        pcm: bytes,
        *,
        rate: int = 16000,
        channels: int = 1,
        speaker_id: str = "",
        speaker_name: str = "",
    ) -> None:
        """What the caller says: raw 16-bit PCM. Send chunks of about 100 ms."""
        await self._ctx.call(
            "calls.audio",
            call_id=call_id,
            pcm=base64.b64encode(pcm).decode("ascii"),
            rate=rate,
            channels=channels,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
        )

    async def video_frame(self, call_id: str) -> dict[str, Any] | None:
        """The newest frame of the SyntH's face as ``{"seq", "width", "height",
        "frame"}`` (``frame`` is planar I420 bytes), or ``None`` for none yet."""
        result = dict(await self._ctx.call("calls.video_frame", call_id=call_id))
        if not result.get("available"):
            return None
        result["frame"] = base64.b64decode(result["frame"])
        return result

    async def end(self, call_id: str) -> bool:
        """The call is over on the platform."""
        return bool((await self._ctx.call("calls.end", call_id=call_id))["ended"])

    def on_play(
        self,
        handler: Callable[
            [str, str, str | None, Path, bool, dict[str, int] | None],
            Awaitable[Any] | Any,
        ],
    ) -> None:
        """Play a file into a call. ``handler(interface_id, call_id, user_id, path,
        speech, video)`` returns the seconds it will take (a number). ``video`` is
        ``{"fps", "width", "height"}`` when the call carries the SyntH's face. ``path``
        is a private copy readable by you and **deleted when the handler returns**: copy
        it if the playback needs the file afterwards. Start playing and return: do not
        wait for the end."""

        async def adapter(params: dict[str, Any]) -> Any:
            result: Any = handler(
                str(params.get("interface_id", "")),
                str(params.get("call_id", "")),
                params.get("user_id"),
                Path(str(params.get("path", ""))),
                bool(params.get("speech")),
                params.get("video"),
            )
            if asyncio.iscoroutine(result):
                result = await result
            return {"seconds": float(cast(float, result) if result else 0.0)}

        self._ctx.handle("call.play", adapter)

    def on_ended(
        self, handler: Callable[[str, str, str | None], Awaitable[Any] | Any]
    ) -> None:
        """The Core ended a call: ``handler(interface_id, call_id, user_id)``. Leave
        the call on the platform."""

        async def adapter(params: dict[str, Any]) -> Any:
            result = handler(
                str(params.get("interface_id", "")),
                str(params.get("call_id", "")),
                params.get("user_id"),
            )
            if asyncio.iscoroutine(result):
                await result
            return {}

        self._ctx.handle("call.ended", adapter)

    def on_video_lost(self, handler: Callable[[str], Awaitable[Any] | Any]) -> None:
        """The SyntH's face is no longer available for ``call_id``: stop sending
        video, the call goes on as audio."""

        async def adapter(params: dict[str, Any]) -> Any:
            result = handler(str(params.get("call_id", "")))
            if asyncio.iscoroutine(result):
                await result
            return {}

        self._ctx.handle("call.video_lost", adapter)


class Panels(_Namespace):
    """Setup panels the Core draws for you (see ``core.extensions.panels``)."""

    async def set(self, interface_id: str, panel: str, state: dict[str, Any]) -> None:
        """Show or update a panel on one of your interfaces. ``state`` is data only:
        ``{"title", "blocks": [{"type": "text|error|form|button", ...}]}``."""
        await self._ctx.call(
            "panels.set", interface_id=interface_id, panel=panel, state=state
        )

    def on_action(
        self,
        handler: Callable[[str, str, str, dict[str, str]], Awaitable[Any] | Any],
    ) -> None:
        """The person pressed a button or sent a form. ``handler(interface_id,
        panel, action, values)`` returns ``{"ok": bool, "error": str}`` (and calls
        :meth:`set` with the next state). ``values`` can hold a code or a password:
        never log them."""

        async def adapter(params: dict[str, Any]) -> Any:
            result = handler(
                str(params.get("interface_id", "")),
                str(params.get("panel", "")),
                str(params.get("action", "")),
                dict(params.get("values") or {}),
            )
            if asyncio.iscoroutine(result):
                result = await result
            return result if isinstance(result, dict) else {"ok": True}

        self._ctx.handle("panel.action", adapter)


class Endpoints(_Namespace):
    """External endpoints you front (capability ``endpoints.manage``). Only names your
    manifest declares in ``provides.endpoints``, and never an endpoint that holds an
    API key."""

    async def get(self, name: str) -> dict[str, Any]:
        """``{"exists": False}`` or ``{"exists", "id", "base_url", "has_key"}``."""
        return dict(await self._ctx.call("endpoints.get", name=name))

    async def ensure(
        self,
        name: str,
        base_url: str,
        *,
        display_label: str = "",
        extra_config: dict[str, Any] | None = None,
        subsystem_map: dict[str, bool] | None = None,
        only_if_registry_empty: bool = False,
        update_base_url: bool = False,
    ) -> dict[str, Any]:
        """Create the endpoint, or fill in what an existing one lacks: ``extra_config``
        keys the operator already set are left alone. ``only_if_registry_empty`` does
        not add an endpoint behind a configured operator; ``update_base_url`` also
        repoints an existing one (use it to heal a known-stale URL, not to override the
        operator)."""
        return dict(
            await self._ctx.call(
                "endpoints.ensure",
                name=name,
                base_url=base_url,
                display_label=display_label,
                extra_config=extra_config or {},
                subsystem_map=subsystem_map or {},
                only_if_registry_empty=only_if_registry_empty,
                update_base_url=update_base_url,
            )
        )


@dataclass
class WebRequest:
    """An HTTP request the Core forwarded to you (under ``/x/<your id>/api/``)."""

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


@dataclass
class WebResponse:
    """Your answer. ``body`` may be bytes, text, or any JSON value (sent as JSON).
    Only ``content-type``, ``cache-control``, ``content-language``, ``etag`` and
    ``location`` headers are passed on: cookies are the Core's."""

    status: int = 200
    body: Any = b""
    headers: dict[str, str] = field(default_factory=dict)

    def _wire(self) -> dict[str, Any]:
        headers = {k.lower(): v for k, v in self.headers.items()}
        if isinstance(self.body, (bytes, bytearray)):
            data = bytes(self.body)
        elif isinstance(self.body, str):
            data = self.body.encode("utf-8")
            headers.setdefault("content-type", "text/plain; charset=utf-8")
        else:
            data = json.dumps(self.body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("content-type", "application/json")
        return {
            "status": self.status,
            "headers": headers,
            "body": base64.b64encode(data).decode("ascii"),
        }


class Web(_Namespace):
    """Web content you add to the Core's server (capabilities ``web.static`` and
    ``web.routes``, declared in ``[provides.web]``)."""

    def on_request(
        self,
        handler: Callable[[WebRequest], Awaitable[WebResponse] | WebResponse],
    ) -> None:
        """Answer HTTP requests to ``/x/<your id>/api/...``. The Core has already
        checked the WebUI token; bodies are limited to about 600 kB each way."""

        async def adapter(params: dict[str, Any]) -> Any:
            request = WebRequest(
                method=str(params.get("method", "GET")),
                path=str(params.get("path", "")),
                query=dict(params.get("query") or {}),
                headers=dict(params.get("headers") or {}),
                body=base64.b64decode(params.get("body") or ""),
            )
            result = handler(request)
            if asyncio.iscoroutine(result):
                result = await result
            if not isinstance(result, WebResponse):
                raise TypeError("a web handler must return a WebResponse")
            return result._wire()

        self._ctx.handle("web.request", adapter)

    def on_websocket(
        self,
        on_open: Callable[[str, str, dict[str, str]], Awaitable[bool] | bool],
        on_message: Callable[[str, str | bytes], Awaitable[Any] | Any],
        on_closed: Callable[[str], Awaitable[Any] | Any] | None = None,
    ) -> None:
        """Serve WebSocket connections to ``/x/<your id>/ws/<path>`` (capability
        ``web.ws``, ``websockets = true``). ``on_open(conn_id, path, query)`` returns
        ``True`` to accept; ``on_message(conn_id, frame)`` gets ``str`` for a text frame
        and ``bytes`` for a binary one; ``on_closed(conn_id)`` runs when it ends. Answer
        with :meth:`ws_send` / :meth:`ws_close`."""

        async def opened(params: dict[str, Any]) -> Any:
            result = on_open(
                str(params["conn_id"]),
                str(params.get("path", "")),
                dict(params.get("query") or {}),
            )
            if asyncio.iscoroutine(result):
                result = await result
            return {"ok": result is True}

        async def message(params: dict[str, Any]) -> Any:
            frame: str | bytes = (
                params["text"]
                if params.get("text") is not None
                else base64.b64decode(params.get("data") or "")
            )
            result = on_message(str(params["conn_id"]), frame)
            if asyncio.iscoroutine(result):
                await result
            return {"ok": True}

        async def closed(params: dict[str, Any]) -> Any:
            if on_closed is not None:
                result = on_closed(str(params["conn_id"]))
                if asyncio.iscoroutine(result):
                    await result
            return {"ok": True}

        self._ctx.handle("web.ws.open", opened)
        self._ctx.handle("web.ws.message", message)
        self._ctx.handle("web.ws.closed", closed)

    async def ws_send(self, conn_id: str, frame: str | bytes) -> bool:
        """Send a text (``str``) or binary (``bytes``) frame. ``False`` if the peer left."""
        if isinstance(frame, str):
            answer = await self._ctx.call("web.ws.send", conn_id=conn_id, text=frame)
        else:
            answer = await self._ctx.call(
                "web.ws.send",
                conn_id=conn_id,
                data=base64.b64encode(frame).decode("ascii"),
            )
        return bool(answer.get("ok"))

    async def ws_close(self, conn_id: str, code: int = 1000) -> None:
        await self._ctx.call("web.ws.close", conn_id=conn_id, code=code)


class Context:
    """What an extension gets: its identity and the API namespaces."""

    def __init__(
        self,
        peer: RpcPeer,
        *,
        extension_id: str,
        data_dir: Path,
        core_api_version: str = "",
        granted: frozenset[str] = frozenset(),
    ) -> None:
        self._peer = peer
        self.extension_id = extension_id
        self.data_dir = data_dir
        self.core_api_version = core_api_version
        self.granted = granted
        self.stopping = asyncio.Event()
        self._subscriptions: dict[str, list[EventCallback]] = {}
        self._background: set[asyncio.Task[None]] = set()
        self.log = Log(self)
        self.config = Config(self)
        self.storage = Storage(self)
        self.stats = Stats(self)
        self.events = Events(self)
        self.interfaces = Interfaces(self)
        self.panels = Panels(self)
        self.calls = Calls(self)
        self.avatar = Avatar(self)
        self.core = CoreInfo(self)
        self.web = Web(self)
        self.endpoints = Endpoints(self)
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any] | Any]] = {}

    def inbox_path(self, name: str = "file") -> Path:
        """A new, unique path under your data directory for a file you are about to
        send to the Core as an attachment. The directory is created for you."""
        inbox = self.data_dir / "inbox"
        inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in name).strip(
            "._"
        )
        return inbox / f"{secrets.token_hex(4)}_{cleaned or 'file'}"

    def handle(
        self, method: str, handler: Callable[[dict[str, Any]], Awaitable[Any] | Any]
    ) -> None:
        """Implement a method the Core may call on this extension."""
        self._handlers[method] = handler

    async def call(
        self, method: str, _timeout: float | None = None, **params: Any
    ) -> Any:
        """Call a Core API method. Prefer the typed namespaces."""
        try:
            return await self._peer.request(method, params or None, timeout=_timeout)
        except RpcError as exc:
            raise _raise_typed(exc) from None

    def fire(self, method: str, params: dict[str, Any]) -> None:
        """Send a notification without waiting (logs and statistics)."""
        task = asyncio.ensure_future(self._notify(method, params))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        try:
            await self._peer.notify(method, params)
        except RpcError:
            pass

    async def _deliver(self, topic: str, payload: Any) -> None:
        for callback in list(self._subscriptions.get(topic, ())):
            try:
                result = callback(topic, payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 - one bad callback must not stop the rest
                self.log.error(
                    f"event callback for {topic!r} failed:\n{traceback.format_exc()}"
                )


def _load_entry(spec: str) -> Callable[[Context], Awaitable[None]]:
    module_name, _, function_name = spec.partition(":")
    module = importlib.import_module(module_name)
    function = getattr(module, function_name or "main")
    if not asyncio.iscoroutinefunction(function):
        raise TypeError(f"{spec} must be an 'async def main(ctx)'")
    return function


async def open_streams(
    in_fd: int, out_fd: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wrap the protocol file descriptors in asyncio streams."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader),
        os.fdopen(in_fd, "rb", buffering=0),
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(out_fd, "wb", buffering=0)
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    return reader, writer


async def run_extension(
    reader: asyncio.StreamReader,
    writer: Any,
    *,
    extension_id: str,
    data_dir: Path,
    entry: str,
    stop_grace: float = DEFAULT_STOP_GRACE,
) -> int:
    """Handshake with the Core, run the extension's ``main`` and return an exit code."""
    ctx: Context | None = None
    main_task: asyncio.Task[None] | None = None

    async def handler(method: str, params: Any, notification: bool) -> Any:
        if method == "lifecycle.ping":
            return {"pong": True}
        if method == "lifecycle.stop":
            assert ctx is not None
            ctx.stopping.set()
            grace = stop_grace
            if isinstance(params, dict) and isinstance(
                params.get("grace"), (int, float)
            ):
                grace = float(params["grace"])
            asyncio.ensure_future(_stop_after(grace))
            return {"stopping": True}
        if method == "events.deliver" and isinstance(params, dict) and ctx is not None:
            await ctx._deliver(str(params.get("topic")), params.get("payload"))  # noqa: SLF001
            return None
        if ctx is not None and method in ctx._handlers:  # noqa: SLF001
            result = ctx._handlers[method](params if isinstance(params, dict) else {})  # noqa: SLF001
            if asyncio.iscoroutine(result):
                result = await result
            return result
        raise RpcError(ErrorCode.METHOD_NOT_FOUND, f"unknown method: {method}")

    async def _stop_after(grace: float) -> None:
        await asyncio.sleep(grace)
        if main_task is not None and not main_task.done():
            main_task.cancel()

    peer = RpcPeer(reader, writer, handler, name=f"ext:{extension_id}")
    serve = asyncio.ensure_future(peer.serve())
    exit_code = 0
    try:
        hello = await peer.request(
            "session.hello",
            {
                "protocol": PROTOCOL_VERSION,
                "sdk_version": __version__,
                "extension_id": extension_id,
            },
            timeout=10,
        )
        ctx = Context(
            peer,
            extension_id=extension_id,
            data_dir=data_dir,
            core_api_version=str(hello.get("core_api_version", "")),
            granted=frozenset(hello.get("granted", ())),
        )
        entry_main = _load_entry(entry)
        main_task = asyncio.ensure_future(entry_main(ctx))
        done, _ = await asyncio.wait(
            {main_task, asyncio.ensure_future(peer.wait_closed())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if main_task in done:
            try:
                main_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                ctx.log.error(f"extension crashed:\n{traceback.format_exc()}")
                await asyncio.sleep(0.05)  # let the log line out
                exit_code = 1
        else:
            main_task.cancel()  # the Core went away
            exit_code = 0
        if exit_code == 0 and not peer.closed:
            await peer.notify("session.bye")
    except RpcError:
        exit_code = 2
    except Exception:  # noqa: BLE001
        exit_code = 1
    finally:
        await peer.close()
        serve.cancel()
        await asyncio.gather(serve, return_exceptions=True)
    return exit_code
