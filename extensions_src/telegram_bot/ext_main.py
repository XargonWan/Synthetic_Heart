"""Telegram bot interface extension.

Talks to the Telegram Bot API with python-telegram-bot (bundled in ``lib/``) and
to the Core only through ``ctx``. It imports nothing from the Core.

Flow: long-poll ``getUpdates`` -> :func:`convert.inbound_from_message` ->
``ctx.interfaces.inbound``; the Core calls back into :meth:`Bridge.send` to deliver
a reply. The Core owns history, commands and the message queue.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import convert
from telegram import Bot
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)

from synth_sdk.runtime import Context

#: How long an unreachable Bot API is retried (seconds, doubling).
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
#: Files larger than this are not downloaded (the Core refuses them anyway).
MAX_DOWNLOAD = 25 * 1024 * 1024
#: Longest wait honoured for a Telegram flood-control answer.
MAX_RETRY_AFTER = 30.0


class Bridge:
    """One bot, one process."""

    def __init__(self, ctx: Context, bot: Bot) -> None:
        self.ctx = ctx
        self.bot = bot

    # ---------------------------------------------------------------- inbound

    async def handle_message(self, message: Any) -> None:
        item = convert.inbound_from_message(message)
        if item is None:
            return
        files: list[Path] = []
        attachments: list[dict[str, Any]] = []
        try:
            for ref in item.media:
                saved = await self._download(ref)
                if saved is not None:
                    files.append(saved)
                    attachments.append(
                        {
                            "path": str(saved),
                            "filename": ref.filename,
                            "mime_type": ref.mime_type,
                        }
                    )
            await self.ctx.interfaces.inbound(
                convert.INTERFACE_ID,
                item.path,
                attachments=attachments or None,
                **item.kwargs(),
            )
            self.ctx.stats.incr("inbound")
        except Exception as exc:  # one bad message must not stop the polling loop
            self.ctx.stats.incr("errors", kind="inbound")
            self.ctx.log.error(f"inbound {item.path}: {type(exc).__name__}: {exc}")
        finally:
            for path in files:
                with contextlib.suppress(OSError):
                    path.unlink()

    async def _download(self, ref: convert.MediaRef) -> Path | None:
        if ref.size is not None and ref.size > MAX_DOWNLOAD:
            self.ctx.log.warning(f"skipping {ref.filename}: {ref.size} bytes")
            return None
        target = self.ctx.inbox_path(ref.filename)
        try:
            remote = await self.bot.get_file(ref.file_id)
            await remote.download_to_drive(target)
        except Exception as exc:  # any failure: the text must still get through
            self.ctx.log.warning(
                f"cannot download {ref.filename}: {type(exc).__name__}: {exc}"
            )
            with contextlib.suppress(OSError):
                Path(target).unlink()
            return None
        return Path(target)

    # --------------------------------------------------------------- outbound

    async def send(
        self, interface_id: str, payload: dict[str, Any], origin: dict[str, Any]
    ) -> dict[str, Any]:
        target = convert.parse_path(payload.get("interface_path"))
        origin_path = convert.parse_path(origin.get("interface_path"))
        if target is None:
            target = origin_path
        if target is None:
            return {"ok": False, "error": "no usable telegram_bot interface_path"}
        chat_id, thread_id = target
        if thread_id is None and origin_path and origin_path[0] == chat_id:
            thread_id = origin_path[1]

        reply_id = convert.usable_reply_id(payload.get("reply_to"), chat_id)
        if (
            reply_id is None
            and payload.get("reply_to") is None
            and origin_path
            and origin_path[0] == chat_id
        ):
            reply_id = convert.usable_reply_id(origin.get("message_id"), chat_id)

        text = str(payload.get("text") or "")
        attachments = list(payload.get("attachments") or [])
        if not text and not attachments:
            return {"ok": False, "error": "nothing to send"}
        try:
            if attachments:
                await self._send_files(
                    chat_id,
                    thread_id,
                    reply_id,
                    text,
                    attachments,
                    voice=bool(payload.get("send_as_voice")),
                )
            else:
                await self._send_text(chat_id, thread_id, reply_id, text)
        except TelegramError as exc:
            self.ctx.stats.incr("errors", kind="outbound")
            self.ctx.log.error(f"send to {chat_id}: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.ctx.stats.incr("outbound")
        return {"ok": True}

    async def _send_text(
        self, chat_id: int, thread_id: int | None, reply_id: int | None, text: str
    ) -> None:
        for chunk in convert.split_text(text):
            await self._with_ladder(
                chat_id,
                thread_id,
                reply_id,
                lambda thread, reply, mode, chunk=chunk: self.bot.send_message(
                    chat_id,
                    chunk,
                    parse_mode=mode,
                    message_thread_id=thread,
                    reply_to_message_id=reply,
                ),
                parse_mode=convert.parse_mode_for(chat_id),
            )
            reply_id = None  # only the first chunk quotes

    async def _send_files(
        self,
        chat_id: int,
        thread_id: int | None,
        reply_id: int | None,
        text: str,
        attachments: list[dict[str, Any]],
        *,
        voice: bool,
    ) -> None:
        caption: str | None = text or None
        if caption and len(caption) > convert.MAX_CAPTION:
            await self._send_text(chat_id, thread_id, reply_id, caption)
            caption, reply_id = None, None
        for item in attachments:
            kind = str(item.get("kind") or "document")
            path = str(item["path"])

            async def call(
                thread: int | None,
                reply: int | None,
                mode: str | None,
                kind: str = kind,
                path: str = path,
                name: str = str(item.get("name") or Path(path).name),
                cap: str | None = caption,
            ) -> Any:
                with open(path, "rb") as handle:
                    common: dict[str, Any] = {
                        "chat_id": chat_id,
                        "caption": cap,
                        "message_thread_id": thread,
                        "reply_to_message_id": reply,
                    }
                    if kind == "audio" and voice:
                        return await self.bot.send_voice(voice=handle, **common)
                    if kind == "audio":
                        return await self.bot.send_audio(audio=handle, **common)
                    if kind == "image":
                        return await self.bot.send_photo(photo=handle, **common)
                    if kind == "video":
                        return await self.bot.send_video(video=handle, **common)
                    return await self.bot.send_document(
                        document=handle, filename=name, **common
                    )

            await self._with_ladder(chat_id, thread_id, reply_id, call, parse_mode=None)
            caption, reply_id = None, None

    async def _with_ladder(
        self,
        chat_id: int,
        thread_id: int | None,
        reply_id: int | None,
        call: Any,
        *,
        parse_mode: str | None,
    ) -> None:
        """Run ``call(thread, reply, parse_mode)``, shedding what Telegram rejects.

        A deleted original makes the reply reference undeliverable and a stale
        topic id makes the thread undeliverable; the text itself must still go
        out, so each is dropped in turn. Broken Markdown falls back to plain text.
        Flood control is waited out once.
        """
        waited = False
        for _ in range(6):
            try:
                await call(thread_id, reply_id, parse_mode)
                return
            except RetryAfter as exc:
                delay = float(getattr(exc, "retry_after", 1) or 1)
                if waited or delay > MAX_RETRY_AFTER:
                    raise
                waited = True
                await asyncio.sleep(delay)
            except BadRequest as exc:
                reason = str(exc).lower()
                if "parse entities" in reason and parse_mode:
                    parse_mode = None
                elif "replied not found" in reason and reply_id is not None:
                    reply_id = None
                elif "thread not found" in reason and thread_id is not None:
                    thread_id = None
                else:
                    raise
        raise BadRequest("could not deliver after dropping stale identifiers")

    # -------------------------------------------------------------- activity

    ACTIVITY = {
        "typing": "typing",
        "record_audio": "record_voice",
        "upload_audio": "upload_voice",
        "upload_file": "upload_document",
    }

    async def activity(self, interface_id: str, path: str, kind: str) -> None:
        """Show "typing..." and the like. Anything else is shown as typing."""
        target = convert.parse_path(path)
        if target is None:
            return
        chat_id, thread_id = target
        try:
            await self.bot.send_chat_action(
                chat_id=chat_id,
                action=self.ACTIVITY.get(kind, "typing"),
                message_thread_id=thread_id,
            )
        except Exception as exc:  # a missing indicator is not worth an error
            self.ctx.log.debug(f"chat action failed: {type(exc).__name__}")

    # ----------------------------------------------------------------- names

    async def resolve_names(
        self, interface_id: str, chat_id: str | None, thread_id: str | None
    ) -> dict[str, Any]:
        """Chat title. Topic names are not available from the Bot API."""
        if not chat_id or not str(chat_id).lstrip("-").isdigit():
            return {}
        try:
            chat = await self.bot.get_chat(int(chat_id))
        except Exception as exc:  # names are a nicety: never fail the caller
            self.ctx.log.debug(f"get_chat({chat_id}) failed: {type(exc).__name__}")
            return {}
        return {
            "chat_name": getattr(chat, "title", None) or getattr(chat, "username", None)
        }


async def poll(bridge: Bridge, ctx: Context, timeout: int) -> None:
    """Long-poll until the Core asks the extension to stop."""
    offset: int | None = None
    delay = BACKOFF_START
    while not ctx.stopping.is_set():
        try:
            updates = await bridge.bot.get_updates(
                offset=offset,
                timeout=timeout,
                allowed_updates=["message"],
                read_timeout=timeout + 10,
            )
            delay = BACKOFF_START
        except (InvalidToken, Forbidden) as exc:
            ctx.log.error(f"Telegram refused the bot token: {exc}")
            await ctx.stopping.wait()
            return
        except (NetworkError, RetryAfter, TelegramError) as exc:
            ctx.log.warning(
                f"getUpdates failed ({type(exc).__name__}); retry in {delay:.0f}s"
            )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ctx.stopping.wait(), delay)
            delay = min(delay * 2, BACKOFF_MAX)
            continue
        for update in updates:
            offset = update.update_id + 1
            if update.message is not None:
                await bridge.handle_message(update.message)


async def main(ctx: Context) -> None:
    token = str(await ctx.config.get("BOTFATHER_TOKEN") or "").strip()
    if not token:
        ctx.log.warning("BOTFATHER_TOKEN is not configured: Telegram is idle")
        await ctx.stopping.wait()
        return
    api = str(
        await ctx.config.get("TELEGRAM_API_URL") or "https://api.telegram.org"
    ).rstrip("/")
    timeout = max(1, int(await ctx.config.get("POLL_TIMEOUT") or 30))

    bot = Bot(token, base_url=f"{api}/bot", base_file_url=f"{api}/file/bot")
    try:
        await bot.initialize()  # getMe
    except (InvalidToken, Forbidden) as exc:
        ctx.log.error(f"Telegram refused the bot token: {exc}")
        await ctx.stopping.wait()
        return
    me = bot.bot
    bridge = Bridge(ctx, bot)
    ctx.interfaces.on_send_message(bridge.send)
    ctx.interfaces.on_resolve_names(bridge.resolve_names)
    ctx.interfaces.on_activity(bridge.activity)
    await ctx.interfaces.register(
        convert.INTERFACE_ID,
        display_name="Telegram Bot",
        bot_id=str(me.id),
        bot_username=me.username,
        path_hint="telegram_bot/<chat_id>[/<thread_id>]",
        description="Telegram bot. Paths: telegram_bot/<chat_id> or telegram_bot/<chat_id>/<topic_id>.",
        resolve_names=True,
        capabilities=["send_file", "send_audio", "send_voice", "activity"],
    )
    ctx.log.info(f"connected as @{me.username}")
    task = asyncio.ensure_future(poll(bridge, ctx, timeout))
    try:
        await ctx.stopping.wait()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        with contextlib.suppress(Exception):
            await bot.shutdown()
