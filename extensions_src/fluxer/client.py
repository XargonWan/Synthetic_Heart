"""Fluxer's REST API and WebSocket gateway, over aiohttp.

The REST client honours rate limits (``429`` + ``retry_after``); the gateway client runs
the HELLO / IDENTIFY / heartbeat / RESUME life cycle and reconnects with backoff, giving
up only on a close code that means the token is wrong.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import convert

log = logging.getLogger("fluxer.client")

OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY, OP_RESUME = 0, 1, 2, 6
OP_RECONNECT, OP_INVALID_SESSION, OP_HELLO, OP_HEARTBEAT_ACK = 7, 9, 10, 11
#: Close codes that must NOT trigger a reconnect (authentication is broken).
FATAL_CLOSE_CODES = {4004}
MAX_RETRY_AFTER = 10.0


class RestError(RuntimeError):
    """A Fluxer REST call failed (``status`` is the HTTP status, 0 for no response)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


class Rest:
    def __init__(self, token: str, api_base: str) -> None:
        self._token = token
        self._base = api_base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": convert.rest_auth_header(self._token)}
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        form: Callable[[], aiohttp.FormData] | None = None,
    ) -> dict[str, Any]:
        session = await self._ensure()
        url = f"{self._base}{path}"
        for _ in range(3):
            kwargs: dict[str, Any] = {}
            if form is not None:
                kwargs["data"] = form()
            elif json_body is not None:
                kwargs["json"] = json_body
            async with session.request(method, url, **kwargs) as response:
                if response.status == 429:
                    try:
                        retry = float((await response.json()).get("retry_after", 1.0))
                    except Exception:  # noqa: BLE001
                        retry = 1.0
                    await asyncio.sleep(min(retry, MAX_RETRY_AFTER))
                    continue
                if response.status >= 400:
                    raise RestError(response.status, (await response.text())[:300])
                if response.status == 204 or response.content_length == 0:
                    return {}
                try:
                    data = await response.json()
                except Exception:  # noqa: BLE001
                    return {}
                return data if isinstance(data, dict) else {}
        raise RestError(429, "rate limited")

    async def get_me(self) -> dict[str, Any]:
        return await self.request("GET", "/users/@me")

    async def send_message(
        self, channel: str, content: str, *, reply_to: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content}
        if reply_to:
            body["message_reference"] = {"message_id": str(reply_to)}
        return await self.request(
            "POST", f"/channels/{channel}/messages", json_body=body
        )

    async def send_file(
        self,
        channel: str,
        path: Path,
        *,
        caption: str | None,
        mime: str,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if caption and caption.strip():
            payload["content"] = caption
        if reply_to:
            payload["message_reference"] = {"message_id": str(reply_to)}
        data = path.read_bytes()

        def form() -> aiohttp.FormData:
            body = aiohttp.FormData()
            body.add_field(
                "payload_json", json.dumps(payload), content_type="application/json"
            )
            body.add_field("files[0]", data, filename=path.name, content_type=mime)
            return body

        return await self.request("POST", f"/channels/{channel}/messages", form=form)

    async def set_avatar(self, data_uri: str | None) -> dict[str, Any]:
        return await self.request("PATCH", "/users/@me", json_body={"avatar": data_uri})


class FatalGatewayError(Exception):
    """The gateway must not be retried (for example a rejected token)."""


Handler = Callable[[dict[str, Any]], Awaitable[Any] | Any]


class Gateway:
    def __init__(
        self,
        token: str,
        url: str,
        *,
        on_message: Handler,
        on_ready: Handler | None = None,
    ) -> None:
        self._token, self._url = token, url
        self._on_message, self._on_ready = on_message, on_ready
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._interval_ms = 0
        self._seq: int | None = None
        self._session_id: str | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()

    async def run(self) -> None:
        """Run with reconnection until stopped or until the token is refused."""
        backoff = 5.0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = 5.0
            except FatalGatewayError as exc:
                log.error("fatal gateway error, not reconnecting: %s", exc)
                raise
            except Exception as exc:  # noqa: BLE001 - network failures
                log.warning("gateway error: %s", type(exc).__name__)
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(self._stop.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)

    async def stop(self) -> None:
        self._stop.set()
        if self._heartbeat:
            self._heartbeat.cancel()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _connect_once(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            async with self._session.ws_connect(self._url) as ws:
                self._ws = ws
                async for message in ws:
                    if self._stop.is_set():
                        break
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await self._handle(message.data)
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise RuntimeError("websocket error")
                    elif message.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        break
                if ws.close_code in FATAL_CLOSE_CODES:
                    raise FatalGatewayError(f"close code {ws.close_code}")
        finally:
            self.connected.clear()
            if self._heartbeat:
                self._heartbeat.cancel()
                self._heartbeat = None
            if self._session is not None and not self._session.closed:
                await self._session.close()
            self._ws = None

    async def _handle(self, raw: str) -> None:
        try:
            packet = json.loads(raw)
        except ValueError:
            return
        if not isinstance(packet, dict):
            return
        op, seq = packet.get("op"), packet.get("s")
        if seq is not None:
            self._seq = seq
        if op == OP_HELLO:
            self._interval_ms = int(
                (packet.get("d") or {}).get("heartbeat_interval", 30000)
            )
            if not self._heartbeat or self._heartbeat.done():
                self._heartbeat = asyncio.ensure_future(self._beat())
            await self._identify()
        elif op == OP_HEARTBEAT:
            await self._send_heartbeat()
        elif op == OP_RECONNECT:
            if self._ws is not None and not self._ws.closed:
                await self._ws.close()
        elif op == OP_INVALID_SESSION:
            self._session_id, self._seq = None, None
            await asyncio.sleep(1 + random.random() * 4)
            await self._identify()
        elif op == OP_DISPATCH:
            await self._dispatch(packet)

    async def _dispatch(self, packet: dict[str, Any]) -> None:
        event, data = packet.get("t"), packet.get("d") or {}
        if event == "READY":
            self._session_id = data.get("session_id")
            self.connected.set()
            await self._call(self._on_ready, data)
        elif event == "MESSAGE_CREATE":
            await self._call(self._on_message, data)

    @staticmethod
    async def _call(handler: Handler | None, data: dict[str, Any]) -> None:
        if handler is None:
            return
        try:
            result = handler(data)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 - one bad event must not drop the gateway
            log.warning("event handler failed: %s", type(exc).__name__)

    async def _beat(self) -> None:
        interval = max(self._interval_ms, 1000) / 1000.0
        jitter = random.random() * 0.5
        while not self._stop.is_set():
            await asyncio.sleep(interval + jitter)
            await self._send_heartbeat()

    async def _send_heartbeat(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_json({"op": OP_HEARTBEAT, "d": self._seq})

    async def _identify(self) -> None:
        if self._ws is None or self._ws.closed:
            return
        token = convert.gateway_token(self._token)
        if self._session_id and self._seq is not None:
            packet = {
                "op": OP_RESUME,
                "d": {"token": token, "session_id": self._session_id, "seq": self._seq},
            }
        else:
            packet = {
                "op": OP_IDENTIFY,
                "d": {
                    "token": token,
                    "properties": {
                        "os": "linux",
                        "browser": "synthetic-heart",
                        "device": "synthetic-heart",
                    },
                },
            }
        await self._ws.send_json(packet)
