"""Telegram user-account interface extension.

Talks to Telegram as a normal user (Telethon, bundled in ``lib/``) and to the Core
only through ``ctx``; it imports nothing from the Core.

Life cycle: the interface is registered at once, disabled, so its setup panel can be
shown in the WebUI. Once the account is logged in (``login.py``) the client connects,
the interface is enabled and incoming messages are handed to the Core with
``ctx.interfaces.inbound``; replies come back through :meth:`Bridge.send`. Voice calls
live in ``calls.py``.

The legacy Telethon userbot prototype (``interface_dev/telethon_userbot.py``) was removed in
commit 35335de3; find it with ``git log --diff-filter=D -- interface_dev/telethon_userbot.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import convert
import login
import tl

from synth_sdk.runtime import Context

#: Files larger than this are not downloaded (the Core refuses them anyway).
MAX_DOWNLOAD = 25 * 1024 * 1024
#: Longest wait honoured for a flood-control answer.
MAX_FLOOD_WAIT = 30.0
#: Messages remembered as handled, so recovery and a re-delivered update never run twice.
SEEN_LIMIT = 4000

ANSWER_CALL = "answer_call_telegram"
log = logging.getLogger("telegram")


class Bridge:
    """One account, one process."""

    def __init__(self, ctx: Context, account: tl.Account) -> None:
        self.ctx = ctx
        self.account = account
        self._seen: OrderedDict[tuple[Any, Any], None] = OrderedDict()

    # --------------------------------------------------------------- config

    async def _int(self, key: str, default: int) -> int:
        try:
            return int(await self.ctx.config.get(key))
        except (TypeError, ValueError):
            return default

    def claim(self, raw: Any) -> bool:
        """Mark a message as handled; ``False`` when it already was."""
        key = (getattr(raw, "chat_id", None), getattr(raw, "id", None))
        if key[1] is None:
            return True
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)
        return True

    # -------------------------------------------------------------- inbound

    async def on_event(self, event: Any) -> None:
        """Telethon ``NewMessage`` entry point."""
        try:
            await self.handle(event.message)
        except Exception as exc:  # one bad message must not stop the client
            self.ctx.stats.incr("errors", kind="inbound")
            self.ctx.log.error(f"inbound: {type(exc).__name__}: {exc}")

    async def handle(self, raw: Any, *, recovered: bool = False) -> bool:
        """Hand one incoming message to the Core. ``False`` when it was skipped."""
        if not self.claim(raw):
            return False
        chat = await raw.get_chat()
        sender = await raw.get_sender()
        text = getattr(raw, "raw_text", None) or ""
        media = convert.media_kinds(raw)
        if sender is None or (not text and not media):
            return False
        thread = convert.thread_of(raw)
        path = convert.build_path(raw.chat_id, thread)
        backlog = convert.is_backlog(
            raw.date,
            datetime.now(timezone.utc),
            float(await self._int("TELEGRAM_BACKLOG_MAX_AGE_SECONDS", 120)),
        )
        reply = await self._reply_of(raw, thread)

        files: list[Path] = []
        attachments: list[dict[str, Any]] = []
        try:
            if not backlog:
                for kind, name, mime in media:
                    saved = await self._download(raw, name)
                    if saved is not None:
                        files.append(saved)
                        attachments.append(
                            {"path": str(saved), "filename": name, "mime_type": mime}
                        )
            sender_name = convert.full_name(sender) or str(sender.id)
            await self.ctx.interfaces.inbound(
                convert.INTERFACE_ID,
                path,
                text=text,
                sender_id=str(sender.id),
                sender_name=sender_name,
                sender_full_name=sender_name,
                sender_username=getattr(sender, "username", None) or None,
                chat_username=getattr(chat, "username", None) or None,
                message_id=str(raw.id),
                chat_id=str(raw.chat_id),
                thread_id=str(thread) if thread else None,
                chat_type=convert.chat_kind(chat),
                chat_title=convert.title_of(chat),
                reply_to=reply,
                attachments=attachments or None,
                is_voice_input=convert.is_spoken(raw) and not backlog,
                timestamp=raw.date.isoformat() if raw.date else None,
                backlog=backlog,
            )
            self.ctx.stats.incr("backlog" if backlog else "inbound")
        finally:
            for file in files:
                with contextlib.suppress(OSError):
                    file.unlink()
        return True

    async def _reply_of(self, raw: Any, thread: int | None) -> dict[str, Any] | None:
        reply = getattr(raw, "reply_to", None)
        if reply is None or not getattr(reply, "reply_to_msg_id", None):
            return None
        # A plain forum-topic message "replies" to the topic header: not a quote.
        if thread and not getattr(reply, "reply_to_top_id", None):
            return None
        try:
            replied = await raw.get_reply_message()
            sender = await replied.get_sender() if replied is not None else None
        except Exception as exc:  # noqa: BLE001 - network, or a deleted message
            self.ctx.log.debug(f"replied message not fetched: {type(exc).__name__}")
            return None
        return convert.reply_block(replied, sender)

    async def _download(self, raw: Any, name: str) -> Path | None:
        doc = getattr(raw, "document", None) or getattr(raw, "file", None)
        size = getattr(doc, "size", None)
        if isinstance(size, int) and size > MAX_DOWNLOAD:
            self.ctx.log.warning(f"skipping {name}: {size} bytes")
            return None
        target = self.ctx.inbox_path(name)
        try:
            saved = await self.account.download(raw, target)
        except Exception as exc:  # any failure: the text must still get through
            self.ctx.log.warning(f"cannot download {name}: {type(exc).__name__}: {exc}")
            with contextlib.suppress(OSError):
                Path(target).unlink()
            return None
        return saved

    async def recover(self) -> int:
        """Handle what arrived while the extension was offline. Returns how many."""
        window = await self._int("TELEGRAM_RECOVERY_WINDOW_SECONDS", 3600)
        if window <= 0:
            return 0
        since = datetime.now(timezone.utc) - timedelta(seconds=window)
        recovered = 0
        async for raw in self.account.recent_incoming(since):
            try:
                if not (getattr(raw, "raw_text", None) or "").strip():
                    continue  # media-only: no history row to tell it from a handled one
                sender = await raw.get_sender()
                if sender is None:
                    continue
                thread = convert.thread_of(raw)
                path = convert.build_path(raw.chat_id, thread)
                if await self.ctx.interfaces.seen(
                    convert.INTERFACE_ID, path, str(sender.id), raw.date.isoformat()
                ):
                    continue
                if await self.handle(raw, recovered=True):
                    recovered += 1
            except Exception as exc:  # noqa: BLE001
                self.ctx.log.warning(
                    f"recovery of a message failed: {type(exc).__name__}"
                )
        if recovered:
            self.ctx.log.info(f"recovered {recovered} message(s) missed while offline")
        return recovered

    # ------------------------------------------------------------- outbound

    async def send(
        self, interface_id: str, payload: dict[str, Any], origin: dict[str, Any]
    ) -> dict[str, Any]:
        target = convert.parse_path(payload.get("interface_path"))
        origin_path = convert.parse_path(origin.get("interface_path"))
        if target is None:
            target = origin_path
        if target is None:
            return {"ok": False, "error": "no usable telegram interface_path"}
        chat, thread = target
        if thread is None and origin_path and origin_path[0] == chat:
            thread = origin_path[1]

        reply = convert.usable_reply_id(payload.get("reply_to"), chat)
        if (
            reply is None
            and payload.get("reply_to") is None
            and origin_path
            and origin_path[0] == chat
        ):
            reply = convert.usable_reply_id(origin.get("message_id"), chat)

        text = str(payload.get("text") or "")
        attachments = list(payload.get("attachments") or [])
        if not text and not attachments:
            return {"ok": False, "error": "nothing to send"}
        try:
            if attachments:
                await self._send_files(
                    chat,
                    thread,
                    reply,
                    text,
                    attachments,
                    voice=bool(payload.get("send_as_voice")),
                )
            else:
                for chunk in convert.split_text(text):
                    await self._ladder(
                        chat,
                        thread,
                        reply,
                        lambda t, r, md, chunk=chunk: self.account.send_text(
                            chat, chunk, reply=r, thread=t, markdown=md
                        ),
                        markdown=convert.parse_mode_for(chat) is not None,
                    )
                    reply = None  # only the first chunk quotes
        except Exception as exc:  # noqa: BLE001 - the Core sees one failed delivery
            self.ctx.stats.incr("errors", kind="outbound")
            self.ctx.log.error(f"send to {chat}: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        self.ctx.stats.incr("outbound")
        return {"ok": True}

    async def _send_files(
        self,
        chat: Any,
        thread: int | None,
        reply: int | None,
        text: str,
        attachments: list[dict[str, Any]],
        *,
        voice: bool,
    ) -> None:
        caption: str | None = text or None
        if caption and len(caption) > convert.MAX_CAPTION:
            await self._ladder(
                chat,
                thread,
                reply,
                lambda t, r, md, body=caption: self.account.send_text(
                    chat, body, reply=r, thread=t, markdown=md
                ),
                markdown=convert.parse_mode_for(chat) is not None,
            )
            caption, reply = None, None
        for item in attachments:
            await self._ladder(
                chat,
                thread,
                reply,
                lambda t, r, md, item=item, cap=caption: self.account.send_file(
                    chat,
                    str(item["path"]),
                    caption=cap,
                    reply=r,
                    thread=t,
                    kind=str(item.get("kind") or "document"),
                    voice=voice,
                ),
                markdown=False,
            )
            caption, reply = None, None

    async def _ladder(
        self,
        chat: Any,
        thread: int | None,
        reply: int | None,
        call: Any,
        *,
        markdown: bool,
    ) -> None:
        """Run ``call(thread, reply, markdown)``, shedding what Telegram rejects.

        A deleted original makes the reply undeliverable and a stale topic makes the
        thread undeliverable; the text must still go out, so each is dropped in turn.
        Broken markup falls back to plain text. Flood control is waited out once.
        """
        waited = False
        for _ in range(6):
            try:
                await call(thread, reply, markdown)
                return
            except Exception as exc:  # noqa: BLE001
                kind, delay = convert.classify_error(exc)
                if kind == convert.FLOOD and not waited and delay <= MAX_FLOOD_WAIT:
                    waited = True
                    await asyncio.sleep(delay)
                elif kind == convert.REPLY_GONE and reply is not None:
                    reply = None
                elif kind == convert.THREAD_GONE and thread is not None:
                    thread = None
                elif kind == convert.BAD_MARKUP and markdown:
                    markdown = False
                else:
                    raise
        raise RuntimeError("could not deliver after dropping stale identifiers")

    # ------------------------------------------------------ small handlers

    async def resolve_names(
        self, interface_id: str, chat_id: str | None, thread_id: str | None
    ) -> dict[str, Any]:
        """Chat title. Topic names are not available without extra requests."""
        if not chat_id or not str(chat_id).lstrip("-").isdigit():
            return {}
        try:
            return {"chat_name": await self.account.chat_title(int(chat_id))}
        except Exception as exc:  # noqa: BLE001 - names are a nicety
            self.ctx.log.debug(f"title lookup failed: {type(exc).__name__}")
            return {}

    async def activity(self, interface_id: str, path: str, kind: str) -> None:
        target = convert.parse_path(path)
        if target is None or isinstance(target[0], str):
            return
        actions = {
            "typing": "typing",
            "record_audio": "record_voice",
            "upload_audio": "upload_voice",
            "upload_file": "upload_document",
        }
        try:
            await self.account.chat_action(
                target[0], actions.get(kind, "typing"), target[1]
            )
        except Exception as exc:  # noqa: BLE001 - a missing indicator is not an error
            self.ctx.log.debug(f"chat action failed: {type(exc).__name__}")

    async def react(
        self, interface_id: str, chat_id: str, message_id: str, emoji: str
    ) -> bool:
        if not chat_id.lstrip("-").isdigit() or not message_id.isdigit():
            return False
        try:
            await self.account.react(int(chat_id), int(message_id), emoji)
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.warning(f"reaction failed: {type(exc).__name__}")
            return False
        return True

    async def set_avatar(
        self, interface_id: str, image: bytes | None, version: str | None, force: bool
    ) -> bool:
        """Set (or remove) the account picture. Only acts when the operator opted in;
        a picture this extension did not set is never deleted."""
        if not await self.ctx.config.get("TELEGRAM_USE_SYNTH_AVATAR"):
            return False
        applied = await self.ctx.storage.get("avatar_applied", "")
        try:
            if image is None:
                if not applied:
                    return False
                await self.account.remove_avatar()
                await self.ctx.storage.set("avatar_applied", "")
                return True
            if version and version == applied and not force:
                return False
            await self.account.set_avatar(image)
            await self.ctx.storage.set("avatar_applied", version or "")
            return True
        except Exception as exc:  # noqa: BLE001 - rate limits, not connected...
            self.ctx.log.warning(f"avatar update failed: {type(exc).__name__}")
            return False


class Runner:
    """Owns the connection: logs in, connects, runs, reconnects after a login change."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.flow = login.LoginFlow()
        self.account: tl.Account | None = None
        self.bridge: Bridge | None = None
        self.calls: Any = None
        self.wake = asyncio.Event()
        self.connected_as: str | None = None

    # ------------------------------------------------------------ the panel

    async def credentials(self) -> tuple[int, str] | None:
        api_id = str(await self.ctx.config.get("TELEGRAM_API_ID") or "").strip()
        api_hash = str(await self.ctx.config.get("TELEGRAM_API_HASH") or "").strip()
        if not api_id.isdigit() or not api_hash:
            return None
        return int(api_id), api_hash

    async def push_panel(self) -> None:
        self.flow.expire_if_stale()
        state = login.panel(
            connected_as=self.connected_as,
            has_credentials=await self.credentials() is not None,
            flow=self.flow,
            phone=str(await self.ctx.config.get("TELEGRAM_PHONE") or ""),
            has_session=bool(await self.ctx.storage.get(login.SESSION_KEY)),
        )
        await self.ctx.panels.set(convert.INTERFACE_ID, "login", state)

    async def on_panel_action(
        self, interface_id: str, panel: str, action: str, values: dict[str, str]
    ) -> dict[str, Any]:
        creds = await self.credentials()
        if action == "send_code" and creds:
            phone = (
                values.get("phone")
                or str(await self.ctx.config.get("TELEGRAM_PHONE") or "")
            ).strip()
            await self.flow.send_code(creds[0], creds[1], phone)
        elif action == "verify_code":
            await self.flow.verify_code(values.get("code", ""))
        elif action == "verify_password":
            await self.flow.verify_password(values.get("password", ""))
        elif action == "cancel":
            await self.flow.cancel()
        elif action == "logout":
            await self.ctx.storage.delete(login.SESSION_KEY)
            await self.ctx.storage.delete("avatar_applied")
            self.flow = login.LoginFlow()
            self.wake.set()
        if self.flow.state == "authorized" and self.flow.session:
            await self.ctx.storage.set(login.SESSION_KEY, self.flow.session)
            self.flow.session = None
            self.wake.set()
        await self.push_panel()
        return {"ok": self.flow.error is None, "error": self.flow.error or ""}

    # ----------------------------------------------------------- connection

    async def register(self, me: Any = None, calls: bool = False) -> None:
        caps = [
            "send_file",
            "send_audio",
            "send_voice",
            "activity",
            "reaction",
            "avatar",
        ]
        if calls:
            caps.append("call")
        await self.ctx.interfaces.register(
            convert.INTERFACE_ID,
            display_name="Telegram (User)",
            bot_id=str(me.id) if me is not None else None,
            bot_username=getattr(me, "username", None) if me is not None else None,
            path_hint="telegram/<@username or chat_id>[/<thread_id>]",
            description=(
                "A real Telegram user account. Paths: telegram/<@username> (preferred when "
                "the person or public group has one), telegram/<chat_id> or "
                "telegram/<chat_id>/<topic_id>. Write like a person, not like a bot."
            ),
            resolve_names=True,
            capabilities=caps,
            actions={ANSWER_CALL: ANSWER_CALL_DEFINITION} if calls else None,
        )

    async def connect(self, session: str, api_id: int, api_hash: str) -> Any:
        """Connect with a stored session. Returns the client, or ``None``."""
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(session), api_id, api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                await self.ctx.interfaces.set_state(
                    convert.INTERFACE_ID,
                    False,
                    "Telegram session expired: log in again",
                )
                return None
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await client.disconnect()
            self.ctx.log.error(f"startup failed: {type(exc).__name__}")
            await self.ctx.interfaces.set_state(
                convert.INTERFACE_ID, False, f"Startup failed: {type(exc).__name__}"
            )
            return None
        self.account = tl.Account(client)
        self.bridge = Bridge(self.ctx, self.account)
        client.add_event_handler(self.bridge.on_event, events.NewMessage(incoming=True))
        return client

    async def run(self) -> None:
        ctx = self.ctx
        await self.register()
        await ctx.interfaces.set_state(
            convert.INTERFACE_ID, False, "Telegram login required"
        )
        await self.push_panel()
        while not ctx.stopping.is_set():
            self.wake.clear()
            creds = await self.credentials()
            session = await ctx.storage.get(login.SESSION_KEY)
            if creds is None or not session:
                await self.idle()
                continue
            client = await self.connect(str(session), creds[0], creds[1])
            if client is None:
                await self.push_panel()
                await self.idle()
                continue
            try:
                await self.serve(client)
            finally:
                await self.stop_calls()
                self.account, self.bridge, self.connected_as = None, None, None
                with contextlib.suppress(Exception):
                    await client.disconnect()
                with contextlib.suppress(Exception):
                    await ctx.interfaces.set_state(
                        convert.INTERFACE_ID, False, "Telegram login required"
                    )
                    await self.push_panel()

    async def idle(self) -> None:
        """Wait for a login change or for the Core to stop us."""
        stopping = asyncio.ensure_future(self.ctx.stopping.wait())
        woke = asyncio.ensure_future(self.wake.wait())
        await asyncio.wait({stopping, woke}, return_when=asyncio.FIRST_COMPLETED)
        for task in (stopping, woke):
            task.cancel()

    async def serve(self, client: Any) -> None:
        ctx = self.ctx
        assert self.account is not None and self.bridge is not None
        me = await self.account.refresh_me()
        self.connected_as = convert.full_name(me) or f"id {me.id}"
        started = await self.start_calls(client)
        await self.register(me, calls=started)
        if started:
            await ctx.calls.provider(
                convert.INTERFACE_ID, video=self.calls.video_available()
            )
        await ctx.interfaces.set_state(convert.INTERFACE_ID, True)
        await self.push_panel()
        ctx.log.info(f"connected as {self.connected_as} (id={me.id})")
        background = [
            asyncio.ensure_future(self.warm_and_recover()),
            asyncio.ensure_future(self.sync_avatar()),
        ]
        ended = asyncio.ensure_future(client.run_until_disconnected())
        stopping = asyncio.ensure_future(ctx.stopping.wait())
        woke = asyncio.ensure_future(self.wake.wait())
        try:
            await asyncio.wait(
                {ended, stopping, woke}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (*background, ended, stopping, woke):
                task.cancel()

    async def warm_and_recover(self) -> None:
        assert self.account is not None and self.bridge is not None
        try:
            await self.account.warm()
            await self.bridge.recover()
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.warning(
                f"missed-message recovery failed: {type(exc).__name__}"
            )

    async def sync_avatar(self) -> None:
        """Bring the account picture in line with the synth avatar after start."""
        assert self.bridge is not None
        try:
            if not await self.ctx.config.get("TELEGRAM_USE_SYNTH_AVATAR"):
                return
            current = await self.ctx.avatar.get()
            if current is None:
                return
            import base64

            await self.bridge.set_avatar(
                convert.INTERFACE_ID,
                base64.b64decode(current["image"]),
                current.get("version"),
                False,
            )
        except Exception as exc:  # noqa: BLE001 - the avatar never breaks startup
            self.ctx.log.warning(f"avatar sync failed: {type(exc).__name__}")

    # ---------------------------------------------------------------- calls

    async def start_calls(self, client: Any) -> bool:
        if not await self.ctx.config.get("TELEGRAM_CALLS_ENABLED"):
            return False
        try:
            import calls

            self.calls = calls.TelegramCalls(self.ctx, convert.INTERFACE_ID)
            return await self.calls.start(client)
        except Exception as exc:  # noqa: BLE001 - calls are optional
            self.ctx.log.warning(f"calls unavailable: {type(exc).__name__}: {exc}")
            self.calls = None
            return False

    async def stop_calls(self) -> None:
        calls, self.calls = self.calls, None
        if calls is not None:
            with contextlib.suppress(Exception):
                await calls.stop()

    async def answer_call_action(
        self,
        interface_id: str,
        name: str,
        payload: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if name != ANSWER_CALL or self.calls is None:
            return {"status": "failed", "message": "Telegram calls are not available"}
        try:
            caller = int(str(payload.get("user_id", "")).strip())
        except ValueError:
            return {"status": "failed", "message": "user_id must be a Telegram id"}
        return await self.calls.answer_pending(caller, bool(payload.get("answer")))

    async def start_call(self, interface_id: str, user_id: str) -> dict[str, str]:
        if self.calls is None or not user_id.lstrip("-").isdigit():
            return {"status": "failed", "message": "Telegram calls are not available"}
        return await self.calls.dial(int(user_id))

    async def end_call(self, interface_id: str, user_id: str) -> dict[str, str]:
        if self.calls is None or not user_id.lstrip("-").isdigit():
            return {"status": "success", "message": "No call"}
        return await self.calls.hangup(int(user_id))


ANSWER_CALL_DEFINITION: dict[str, Any] = {
    "description": (
        "Answer or decline a Telegram voice call that is ringing. Only for a call you "
        "were told about and the trainer decided on."
    ),
    "fields": {
        "user_id": {
            "type": "string",
            "example": "123456789",
            "description": "The Telegram id of the caller, from the notice.",
        },
        "answer": {
            "type": "boolean",
            "example": True,
            "description": "true to pick up, false to decline.",
        },
    },
    "notes": [
        "The call rings for under a minute: act as soon as the trainer has decided.",
        "Never pick up a call from someone the trainer did not approve.",
    ],
}


async def main(ctx: Context) -> None:
    runner = Runner(ctx)
    ctx.panels.on_action(runner.on_panel_action)
    ctx.interfaces.on_send_message(lambda i, p, o: _bridge(runner).send(i, p, o))
    ctx.interfaces.on_resolve_names(
        lambda i, c, t: _bridge(runner).resolve_names(i, c, t)
    )
    ctx.interfaces.on_activity(lambda i, p, k: _bridge(runner).activity(i, p, k))
    ctx.interfaces.on_reaction(lambda i, c, m, e: _bridge(runner).react(i, c, m, e))
    ctx.interfaces.on_set_avatar(
        lambda i, img, v, f: _bridge(runner).set_avatar(i, img, v, f)
    )
    ctx.interfaces.on_action(runner.answer_call_action)
    ctx.interfaces.on_call_control(runner.start_call, runner.end_call)
    await runner.run()


def _bridge(runner: Runner) -> Bridge:
    if runner.bridge is None:
        raise RuntimeError("Telegram is not connected")
    return runner.bridge
