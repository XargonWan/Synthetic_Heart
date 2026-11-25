"""Fluxer interface extension.

A Discord-compatible chat platform: REST for sending, a WebSocket gateway for receiving
(aiohttp, bundled in ``lib/``). Talks to the Core only through ``ctx``; imports nothing
from the Core. A Fluxer path is ``fluxer_bot/<guild>/<channel>`` (or ``fluxer_bot/<channel>``
in a direct message): the *channel* is the chat, which is why every inbound message names
its ``chat_id`` explicitly.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from pathlib import Path
from typing import Any

import client
import convert

from synth_sdk.runtime import Context

MAX_FILE_BYTES = 25 * 1024 * 1024


class Bridge:
    def __init__(self, ctx: Context, rest: client.Rest) -> None:
        self.ctx, self.rest = ctx, rest
        self.self_id: str | None = None

    # -------------------------------------------------------------- inbound

    async def on_ready(self, data: dict[str, Any]) -> None:
        user = data.get("user") or {}
        if user.get("id") is not None:
            self.self_id = str(user["id"])

    async def on_message(self, data: dict[str, Any]) -> None:
        message = data.get("message") if isinstance(data.get("message"), dict) else data
        message = message or {}
        author = message.get("author") or {}
        if author.get("id") is not None and str(author["id"]) == str(self.self_id):
            return  # our own message
        if author.get("bot") is True:
            return  # another bot: no loops
        text = message.get("content") or ""
        channel = message.get("channel_id")
        if not isinstance(text, str) or not text.strip() or channel is None:
            return
        channel = str(channel)
        guild = message.get("guild_id")
        name = convert.author_name(author)
        try:
            await self.ctx.interfaces.inbound(
                convert.INTERFACE_ID,
                convert.build_path(guild, channel),
                text=text,
                sender_id=str(author["id"])
                if author.get("id") is not None
                else "unknown",
                sender_name=name,
                sender_full_name=author.get("global_name") or name,
                message_id=str(message["id"])
                if message.get("id") is not None
                else None,
                chat_id=channel,
                chat_type="group" if guild is not None else "private",
                chat_title=channel,
                reply_to=convert.reply_block(message),
                timestamp=message.get("timestamp"),
            )
            self.ctx.stats.incr("inbound")
        except Exception as exc:  # noqa: BLE001
            self.ctx.stats.incr("errors", kind="inbound")
            self.ctx.log.error(f"inbound: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------- outbound

    async def send(
        self, interface_id: str, payload: dict[str, Any], origin: dict[str, Any]
    ) -> dict[str, Any]:
        channel = convert.channel_of(
            payload.get("interface_path")
        ) or convert.channel_of(origin.get("interface_path"))
        if channel is None:
            return {"ok": False, "error": "no usable fluxer_bot interface_path"}
        reply = convert.usable_reply_id(payload.get("reply_to"), channel)
        if (
            reply is None
            and payload.get("reply_to") is None
            and convert.channel_of(origin.get("interface_path")) == channel
        ):
            reply = convert.usable_reply_id(origin.get("message_id"), channel)
        text = str(payload.get("text") or "")
        attachments = list(payload.get("attachments") or [])
        if not text.strip() and not attachments:
            return {"ok": False, "error": "nothing to send"}
        try:
            if attachments:
                caption: str | None = text or None
                for item in attachments:
                    await self._with_stale_reply(
                        reply,
                        lambda r, item=item, cap=caption: self.rest.send_file(
                            channel,
                            Path(str(item["path"])),
                            caption=cap,
                            mime=str(
                                item.get("mime_type") or "application/octet-stream"
                            ),
                            reply_to=r,
                        ),
                    )
                    caption, reply = None, None
            else:
                for chunk in convert.split_text(text):
                    await self._with_stale_reply(
                        reply,
                        lambda r, chunk=chunk: self.rest.send_message(
                            channel, chunk, reply_to=r
                        ),
                    )
                    reply = None
        except Exception as exc:  # noqa: BLE001 - the Core sees one failed delivery
            self.ctx.stats.incr("errors", kind="outbound")
            self.ctx.log.error(f"send to {channel}: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        self.ctx.stats.incr("outbound")
        return {"ok": True}

    @staticmethod
    async def _with_stale_reply(reply: str | None, call: Any) -> None:
        """Send; if Fluxer rejects the quoted message, send without the quote."""
        try:
            await call(reply)
        except client.RestError as exc:
            if reply is not None and exc.status in (400, 404):
                await call(None)
            else:
                raise

    async def resolve_names(
        self, interface_id: str, chat_id: str | None, thread_id: str | None
    ) -> dict[str, Any]:
        # Fluxer v1 has no cheap channel-name lookup: the id is the name.
        return {"chat_name": str(chat_id)} if chat_id else {}

    async def set_avatar(
        self, interface_id: str, image: bytes | None, version: str | None, force: bool
    ) -> bool:
        if not await self.ctx.config.get("FLUXER_USE_SYNTH_AVATAR"):
            return False
        data_uri = (
            None
            if image is None
            else "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        )
        try:
            await self.rest.set_avatar(data_uri)
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.warning(f"avatar update failed: {type(exc).__name__}")
            return False
        return True


async def main(ctx: Context) -> None:
    token = str(await ctx.config.get("FLUXER_TOKEN") or "").strip()
    if not token:
        ctx.log.warning("FLUXER_TOKEN is not configured: Fluxer is idle")
        await ctx.stopping.wait()
        return
    api = convert.resolve_api_base(
        str(await ctx.config.get("FLUXER_API_BASE_URL") or ""),
        await ctx.config.get("FLUXER_API_VERSION"),
    )
    gateway_url = str(await ctx.config.get("FLUXER_GATEWAY_URL") or "").strip()
    rest = client.Rest(token, api)
    bridge = Bridge(ctx, rest)
    ctx.interfaces.on_send_message(bridge.send)
    ctx.interfaces.on_resolve_names(bridge.resolve_names)
    ctx.interfaces.on_set_avatar(bridge.set_avatar)
    try:
        me = await rest.get_me()
        bridge.self_id = str(me["id"]) if me.get("id") is not None else None
    except Exception as exc:  # noqa: BLE001
        ctx.log.warning(f"could not read own identity: {type(exc).__name__}")
    await ctx.interfaces.register(
        convert.INTERFACE_ID,
        display_name="Fluxer Bot",
        bot_id=bridge.self_id,
        path_hint="fluxer_bot/<channel_id> or fluxer_bot/<guild_id>/<channel_id>",
        description=(
            "Fluxer is a Discord-like chat platform. Messages go to channels identified by a "
            "numeric channel id. To reply where a message came from, reuse its interface_path."
        ),
        resolve_names=True,
        capabilities=["send_file", "avatar"],
    )
    gateway = client.Gateway(
        token, gateway_url, on_message=bridge.on_message, on_ready=bridge.on_ready
    )
    task = asyncio.ensure_future(gateway.run())
    stopping = asyncio.ensure_future(ctx.stopping.wait())
    try:
        done, _ = await asyncio.wait(
            {task, stopping}, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done and task.exception() is not None:
            ctx.log.error(
                "the gateway refused the token: Fluxer is idle until it is fixed"
            )
            await ctx.interfaces.set_state(
                convert.INTERFACE_ID, False, "Fluxer refused the token"
            )
            await ctx.stopping.wait()
    finally:
        stopping.cancel()
        await gateway.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await rest.close()
