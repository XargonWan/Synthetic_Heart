"""A small fake of an AzuraCast station: now-playing API and the WebDJ socket."""

from __future__ import annotations

import json
from typing import Any

from aiohttp import WSMsgType, web


class FakeAzuraCast:
    def __init__(self, *, duration: int = 200, elapsed: int = 197) -> None:
        self.now: dict[str, Any] = {
            "id": "s1",
            "title": "I Tried",
            "artist": "Bone Thugs",
            "duration": duration,
            "elapsed": elapsed,
            "playlist": "default",
        }
        self.listeners = 2
        self.api_calls: list[str] = []
        #: What the WebDJ connection received: JSON texts and audio bytes.
        self.webdj_text: list[dict[str, Any]] = []
        self.webdj_bytes = 0
        self.webdj_subprotocols: list[str] = []
        self._runner: web.AppRunner | None = None
        self.base = ""

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/nowplaying/{station}", self._nowplaying)
        app.router.add_get("/api/station/{station}/queue", self._empty_list)
        app.router.add_get("/api/station/{station}/schedule", self._empty_list)
        app.router.add_post("/api/station/{station}/nowplaying/update", self._update)
        app.router.add_get("/webdj/{station}/", self._webdj)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]  # noqa: SLF001
        self.base = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _nowplaying(self, request: web.Request) -> web.Response:
        self.api_calls.append(request.path)
        n = self.now
        return web.json_response(
            {
                "station": {"name": "Radio Dee"},
                "listeners": {"total": self.listeners},
                "now_playing": {
                    "song": {"id": n["id"], "title": n["title"], "artist": n["artist"]},
                    "playlist": n["playlist"],
                    "duration": n["duration"],
                    "elapsed": n["elapsed"],
                    "remaining": n["duration"] - n["elapsed"],
                },
                "playing_next": {
                    "song": {"title": "Next Song", "artist": "Next Artist"}
                },
            }
        )

    async def _empty_list(self, request: web.Request) -> web.Response:
        return web.json_response([])

    async def _update(self, request: web.Request) -> web.Response:
        self.api_calls.append(f"update {await request.text()}")
        return web.json_response({})

    async def _webdj(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(protocols=("webcast",))
        await ws.prepare(request)
        self.webdj_subprotocols.append(ws.ws_protocol or "")
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                self.webdj_text.append(json.loads(message.data))
            elif message.type == WSMsgType.BINARY:
                self.webdj_bytes += len(message.data)
        return ws
