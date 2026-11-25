"""A small fake of a Fluxer instance: REST API and WebSocket gateway on one port."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import WSMsgType, web

TOKEN = "fluxer-test-token"
BOT = {"id": "9000", "username": "synth_bot", "bot": True}


class FakeFluxer:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.calls: list[dict[str, Any]] = []
        self.identified: list[dict[str, Any]] = []
        self.resumed: list[dict[str, Any]] = []
        self.heartbeats = 0
        self._failures: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self._sockets: list[web.WebSocketResponse] = []
        self._seq = 0
        self.close_code: int | None = None
        self.session_id = "sess-1"
        self._runner: web.AppRunner | None = None
        self.base = ""
        self.gateway_url = ""
        self.next_message_id = 7000

    def fail(self, route: str, status: int, body: dict[str, Any] | None = None) -> None:
        """Make the next call to ``route`` (``METHOD path-pattern``) answer ``status``."""
        self._failures.setdefault(route, []).append((status, body or {}))

    def sent(self, kind: str = "messages") -> list[dict[str, Any]]:
        return [c for c in self.calls if c["kind"] == kind and not c.get("failed")]

    async def start(self) -> None:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/gateway", self._gateway)
        app.router.add_get("/api/v1/users/@me", self._me)
        app.router.add_patch("/api/v1/users/@me", self._patch_me)
        app.router.add_post("/api/v1/channels/{channel}/messages", self._post_message)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]  # noqa: SLF001
        self.base = f"http://127.0.0.1:{port}/api/v{{v}}"
        self.gateway_url = f"ws://127.0.0.1:{port}/gateway"

    async def stop(self) -> None:
        for ws in list(self._sockets):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    # ----------------------------------------------------------------- REST

    def _authorised(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bot {self.token}"

    def _maybe_fail(self, route: str, record: dict[str, Any]) -> web.Response | None:
        queue = self._failures.get(route)
        if queue:
            status, body = queue.pop(0)
            record["failed"] = True
            return web.json_response(body, status=status)
        return None

    async def _me(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({}, status=401)
        return web.json_response(BOT)

    async def _patch_me(self, request: web.Request) -> web.Response:
        record = {"kind": "avatar", "body": await request.json()}
        self.calls.append(record)
        failed = self._maybe_fail("PATCH me", record)
        return failed if failed is not None else web.json_response(BOT)

    async def _post_message(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({}, status=401)
        channel = request.match_info["channel"]
        if request.content_type.startswith("multipart/"):
            payload: dict[str, Any] = {}
            files: dict[str, bytes] = {}
            names: dict[str, str] = {}
            async for part in await request.multipart():  # type: ignore[union-attr]
                data = bytes(await part.read())  # type: ignore[arg-type]
                if part.filename is not None:
                    files[part.name or ""] = data
                    names[part.name or ""] = part.filename
                elif part.name == "payload_json":
                    payload = json.loads(data)
            record = {
                "kind": "files",
                "channel": channel,
                "payload": payload,
                "files": files,
                "names": names,
            }
        else:
            record = {
                "kind": "messages",
                "channel": channel,
                "body": await request.json(),
            }
        self.calls.append(record)
        failed = self._maybe_fail("POST messages", record)
        if failed is not None:
            return failed
        self.next_message_id += 1
        return web.json_response(
            {"id": str(self.next_message_id), "channel_id": channel}
        )

    # -------------------------------------------------------------- gateway

    async def _gateway(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._sockets.append(ws)
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": 60000}})
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            packet = json.loads(message.data)
            if packet["op"] == 2:
                self.identified.append(packet["d"])
                if packet["d"]["token"] != self.token:
                    await ws.close(code=4004)
                    break
                self._seq += 1
                await ws.send_json(
                    {
                        "op": 0,
                        "t": "READY",
                        "s": self._seq,
                        "d": {"session_id": self.session_id, "user": BOT},
                    }
                )
            elif packet["op"] == 6:
                self.resumed.append(packet["d"])
            elif packet["op"] == 1:
                self.heartbeats += 1
        self.close_code = ws.close_code
        return ws

    async def dispatch(self, event: str, data: dict[str, Any]) -> None:
        self._seq += 1
        for ws in list(self._sockets):
            if not ws.closed:
                await ws.send_json({"op": 0, "t": event, "s": self._seq, "d": data})

    async def reconnect(self) -> None:
        for ws in list(self._sockets):
            if not ws.closed:
                await ws.send_json({"op": 7, "d": None})

    def sockets_open(self) -> int:
        return sum(1 for ws in self._sockets if not ws.closed)


def message(
    text: str,
    *,
    channel: str = "500",
    guild: str | None = "100",
    author_id: str = "42",
    username: str = "alice",
    message_id: str = "300",
    **extra: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": message_id,
        "channel_id": channel,
        "content": text,
        "author": {"id": author_id, "username": username, "global_name": "Alice A."},
        "timestamp": "2026-01-01T00:00:00+00:00",
        **extra,
    }
    if guild is not None:
        out["guild_id"] = guild
    return out


async def wait_until(predicate: Any, timeout: float = 10.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)
