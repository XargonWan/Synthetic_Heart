"""Matrix interface extension.

Talks to a Matrix homeserver with ``matrix-nio`` (bundled in ``lib/``). Talks to the Core only
through ``ctx``; imports nothing from the Core. A Matrix path is ``matrix/<room>`` or
``matrix/<room>/<thread event>``: the *room* is the chat and a thread is a thread, so
the default ``chat[/thread]`` shape of an interface fits.

It replaces the in-tree ``interface/matrix_interface``, removed in commit 02b10a6e; the old code
is found with ``git log --diff-filter=D -- interface/matrix_interface/matrix_interface.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import convert
from synth_sdk.runtime import Context

try:  # the library is in lib/ in a real install
    from nio import (
        AsyncClient,
        AsyncClientConfig,
        InviteMemberEvent,
        LocalProtocolError,
        LoginResponse,
        RoomMessageNotice,
        RoomMessageText,
        UploadResponse,
    )
except ImportError:  # pragma: no cover - only in a broken install
    AsyncClient = None  # type: ignore[assignment,misc]


class Bridge:
    """One Matrix account, one process."""

    def __init__(self, ctx: Context, client: Any, settings: dict[str, Any]) -> None:
        self.ctx, self.client, self.settings = ctx, client, settings
        self.user_id = str(settings["MATRIX_USER"])
        self.allowed_rooms = convert.parse_rooms(settings.get("MATRIX_ALLOWED_ROOMS"))
        self.trusted = convert.parse_rooms(settings.get("MATRIX_TRUSTED_USERS"))
        self.invite_policy = str(settings.get("MATRIX_INVITE_POLICY") or "trainer_only")
        self.private_policy = str(
            settings.get("MATRIX_PRIVATE_MESSAGES") or "trainer_only"
        )
        self.auto_join = settings.get("MATRIX_AUTO_JOIN") is not False

    async def _trainer(self) -> str | None:
        try:
            return await self.ctx.interfaces.trainer(convert.INTERFACE_ID)
        except Exception:  # noqa: BLE001
            return None

    # -------------------------------------------------------------- inbound

    async def on_invite(self, room: Any, event: Any) -> None:
        if getattr(event, "membership", "") != "invite":
            return
        inviter = getattr(event, "sender", None)
        if not self.auto_join:
            self.ctx.log.debug(
                f"auto-join is off; invite to {room.room_id} from {inviter} ignored"
            )
            return
        if not convert.invite_allowed(
            self.invite_policy, inviter, await self._trainer(), self.trusted
        ):
            self.ctx.log.debug(
                f"invite from {inviter} ignored by invite policy {self.invite_policy}"
            )
            return
        try:
            await self.client.join(room.room_id)
            self.ctx.log.info(f"joined room {room.room_id}")
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.warning(
                f"could not join {getattr(room, 'room_id', '?')}: {type(exc).__name__}"
            )

    async def on_message(self, room: Any, event: Any) -> None:
        sender = str(getattr(event, "sender", "") or "")
        if not room or sender == str(getattr(self.client, "user_id", self.user_id)):
            return
        text = getattr(event, "body", "") or ""
        room_id = getattr(room, "room_id", None)
        if not text.strip() or not room_id:
            return
        alias = getattr(room, "canonical_alias", None)
        if not convert.room_allowed(self.allowed_rooms, room_id, alias):
            self.ctx.log.debug(
                f"ignoring a message from {room_id} (not in the allow list)"
            )
            return
        member_count = getattr(room, "member_count", None)
        kind = convert.chat_type(member_count)
        if kind == "private" and not convert.private_allowed(
            self.private_policy, sender, await self._trainer(), self.trusted
        ):
            self.ctx.log.debug(
                f"ignoring a private message from {sender} (trainer_only)"
            )
            return

        stamp = getattr(event, "server_timestamp", None)
        when = (
            datetime.fromtimestamp(stamp / 1000.0, tz=timezone.utc)
            if stamp is not None
            else datetime.now(tz=timezone.utc)
        )
        source = getattr(event, "source", {}) or {}
        content = source.get("content", {}) if isinstance(source, dict) else {}
        rel = content.get("m.relates_to", {}) if isinstance(content, dict) else {}
        thread_event = (
            rel.get("event_id")
            if isinstance(rel, dict) and rel.get("rel_type") == "m.thread"
            else None
        )
        reply_payload = rel.get("m.in_reply_to", {}) if isinstance(rel, dict) else {}
        reply_event = (
            reply_payload.get("event_id") if isinstance(reply_payload, dict) else None
        )

        reply_block: dict[str, Any] | None = None
        if reply_event:
            parsed = convert.parse_reply_fallback(text)
            if parsed is not None:
                text, quoted = parsed
                reply_block = {
                    "sender_name": convert.quoted_sender_from_fallback(quoted),
                    "text": quoted,
                    "message_id": reply_event,
                }
        try:
            await self.ctx.interfaces.inbound(
                convert.INTERFACE_ID,
                convert.build_path(room_id, thread_event),
                text=text,
                sender_id=sender or "unknown",
                sender_name=convert.extract_username(sender),
                sender_username=convert.extract_username(sender),
                message_id=getattr(event, "event_id", None),
                chat_id=room_id,
                thread_id=thread_event,
                chat_type=kind,
                chat_title=getattr(room, "display_name", None) or alias or room_id,
                chat_username=alias,
                reply_to=reply_block,
                timestamp=when.isoformat(),
                human_count=member_count,
            )
            self.ctx.stats.incr("inbound")
        except Exception as exc:  # noqa: BLE001
            self.ctx.stats.incr("errors", kind="inbound")
            self.ctx.log.error(f"inbound: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------- outbound

    async def _resolve_room(self, room: str) -> str:
        """A ``#alias`` resolved to its room id; other identifiers pass through."""
        if room.startswith("#"):
            try:
                response = await self.client.room_resolve_alias(room)
                if getattr(response, "room_id", None):
                    return str(response.room_id)
            except Exception as exc:  # noqa: BLE001
                self.ctx.log.warning(f"could not resolve {room}: {type(exc).__name__}")
        return room

    async def _room_send(self, room_id: str, content: dict[str, Any]) -> bool:
        response = await self.client.room_send(
            room_id=room_id, message_type="m.room.message", content=content
        )
        # nio returns an ErrorResponse (rather than raising) when a send fails: a missing
        # event id is a failure, so the outcome is reported truthfully.
        return bool(getattr(response, "event_id", None))

    async def send(
        self, interface_id: str, payload: dict[str, Any], origin: dict[str, Any]
    ) -> dict[str, Any]:
        target = convert.split_path(
            payload.get("interface_path")
        ) or convert.split_path(origin.get("interface_path"))
        if target is None:
            return {"ok": False, "error": "no usable matrix interface_path"}
        room, thread = target
        reply = payload.get("reply_to")
        if (
            reply is None
            and origin.get("message_id")
            and convert.split_path(origin.get("interface_path")) == target
        ):
            reply = origin.get("message_id")
        text = str(payload.get("text") or "")
        attachments = list(payload.get("attachments") or [])
        if not text.strip() and not attachments:
            return {"ok": False, "error": "nothing to send"}
        try:
            await self._login()
            room_id = await self._resolve_room(room)
            reply_id = str(reply) if reply else None
            if attachments:
                caption: str | None = text or None
                for item in attachments:
                    await self._send_file(room_id, item, thread, reply_id)
                    if caption:
                        await self._room_send(
                            room_id,
                            {
                                "msgtype": "m.text",
                                "body": caption,
                                **self._rel(thread, None),
                            },
                        )
                        caption = None
                    reply_id = None
            else:
                content: dict[str, Any] = {
                    "msgtype": "m.text",
                    "body": text,
                    **self._rel(thread, reply_id),
                }
                if not await self._room_send(room_id, content):
                    raise RuntimeError("the homeserver did not accept the message")
        except Exception as exc:  # noqa: BLE001 - the Core sees one failed delivery
            self.ctx.stats.incr("errors", kind="outbound")
            self.ctx.log.error(f"send to {room}: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        self.ctx.stats.incr("outbound")
        return {"ok": True}

    @staticmethod
    def _rel(thread: str | None, reply: str | None) -> dict[str, Any]:
        block = convert.relates_to(thread, reply)
        return {"m.relates_to": block} if block else {}

    async def _send_file(
        self, room_id: str, item: dict[str, Any], thread: str | None, reply: str | None
    ) -> None:
        path = Path(str(item["path"]))
        mime = str(item.get("mime_type") or "application/octet-stream")
        size = path.stat().st_size

        def provider(_429: int, _timeouts: int) -> Any:
            return open(path, "rb")

        upload, _keys = await self.client.upload(
            provider,
            content_type=mime,
            filename=str(item.get("name") or path.name),
            filesize=size,
        )
        uri = getattr(upload, "content_uri", None)
        if not uri:
            raise RuntimeError("the upload returned no content URI")
        content = {
            "msgtype": convert.msgtype_for(str(item.get("kind") or "document")),
            "body": str(item.get("name") or path.name),
            "url": uri,
            "info": {"mimetype": mime, "size": size},
            **self._rel(thread, reply),
        }
        if not await self._room_send(room_id, content):
            raise RuntimeError("the homeserver did not accept the file")

    async def activity(self, interface_id: str, path: str, kind: str) -> None:
        """Show "typing..." (Matrix has one indicator for every kind of work)."""
        target = convert.split_path(path)
        if target is not None:
            with contextlib.suppress(Exception):
                await self.client.room_typing(target[0], True, timeout=6000)

    async def resolve_names(
        self, interface_id: str, chat_id: str | None, thread_id: str | None
    ) -> dict[str, Any]:
        room = getattr(self.client, "rooms", {}).get(str(chat_id)) if chat_id else None
        if room is None:
            return {}
        name = (
            getattr(room, "display_name", None)
            or getattr(room, "canonical_alias", None)
            or room.room_id
        )
        return {"chat_name": str(name)}

    async def set_avatar(
        self, interface_id: str, image: bytes | None, version: str | None, force: bool
    ) -> bool:
        if not await self.ctx.config.get("MATRIX_USE_SYNTH_AVATAR") or image is None:
            return False  # Matrix has no portable "remove avatar"
        try:
            upload, _keys = await self.client.upload(
                io.BytesIO(image),
                content_type="image/png",
                filename="avatar.png",
                filesize=len(image),
            )
            if not isinstance(upload, UploadResponse):
                return False
            result = await self.client.set_avatar(upload.content_uri)
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.warning(f"avatar update failed: {type(exc).__name__}")
            return False
        return type(result).__name__ != "ProfileSetAvatarError"

    # ---------------------------------------------------------------- login

    _logged_in = False

    async def _login(self) -> None:
        if self._logged_in or self.client.access_token:
            self._logged_in = True
            return
        password = str(self.settings.get("MATRIX_PASSWORD") or "")
        if not password:
            raise RuntimeError("no MATRIX_PASSWORD or MATRIX_ACCESS_TOKEN configured")
        try:
            response = await self.client.login(
                password,
                device_name=str(self.settings.get("MATRIX_DEVICE_NAME") or "SyntH"),
            )
        except LocalProtocolError as exc:
            raise RuntimeError(f"login protocol error: {exc}") from exc
        if not isinstance(response, LoginResponse):
            raise RuntimeError(f"login refused: {type(response).__name__}")
        self.client.user_id = response.user_id or self.user_id
        self._logged_in = True
        self.ctx.log.info(f"logged in as {self.client.user_id}")

    async def sync_forever(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            try:
                await self.client.sync(timeout=30000, full_state=False)
            except Exception as exc:  # noqa: BLE001 - network failure: try again
                self.ctx.log.error(f"sync: {type(exc).__name__}: {exc}")
                await asyncio.sleep(5)


async def main(ctx: Context) -> None:
    settings = await ctx.config.all()
    homeserver = str(settings.get("MATRIX_HOMESERVER") or "").rstrip("/")
    user = str(settings.get("MATRIX_USER") or "")
    token = settings.get("MATRIX_ACCESS_TOKEN") or None
    if AsyncClient is None:
        ctx.log.error("matrix-nio is not installed: Matrix is idle")
        await ctx.stopping.wait()
        return
    if not homeserver or not user or not (token or settings.get("MATRIX_PASSWORD")):
        ctx.log.warning(
            "MATRIX_HOMESERVER, MATRIX_USER and a password or access token are needed: Matrix is idle"
        )
        await ctx.stopping.wait()
        return
    store = ctx.data_dir / "store"
    store.mkdir(parents=True, exist_ok=True)
    client = AsyncClient(
        homeserver,
        user,
        device_id=settings.get("MATRIX_DEVICE_ID") or None,
        store_path=str(store),
        config=AsyncClientConfig(store_sync_tokens=True),
    )
    if token:
        client.access_token = str(token)
        client.user_id = user
    bridge = Bridge(ctx, client, settings)
    client.add_event_callback(bridge.on_message, RoomMessageText)
    client.add_event_callback(bridge.on_message, RoomMessageNotice)
    # nio dispatches invitations through the same callback list (its stubs say Event only)
    client.add_event_callback(bridge.on_invite, InviteMemberEvent)  # ty: ignore[invalid-argument-type]
    ctx.interfaces.on_send_message(bridge.send)
    ctx.interfaces.on_activity(bridge.activity)
    ctx.interfaces.on_resolve_names(bridge.resolve_names)
    ctx.interfaces.on_set_avatar(bridge.set_avatar)
    await ctx.interfaces.register(
        convert.INTERFACE_ID,
        display_name="Matrix",
        bot_username=convert.extract_username(user),
        path_hint="matrix/<room_id>[/<thread_event_id>]",
        description=(
            "Matrix rooms. Messages go to a room id (or #alias) with an optional thread event. "
            "To reply where a message came from, reuse its interface_path."
        ),
        resolve_names=True,
        capabilities=["send_file", "activity", "avatar"],
    )
    sync = asyncio.ensure_future(bridge.sync_forever(ctx.stopping))
    try:
        try:
            await bridge._login()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            ctx.log.error(f"login failed: {exc}")
            await ctx.interfaces.set_state(
                convert.INTERFACE_ID, False, f"Matrix login failed: {exc}"[:200]
            )
            await ctx.stopping.wait()
            return
        await ctx.stopping.wait()
    finally:
        ctx.stopping.set()
        sync.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sync
        with contextlib.suppress(Exception):
            await client.close()
