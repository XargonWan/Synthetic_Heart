"""A small fake of the Telegram Bot API for testing the Telegram extension.

Speaks just enough of the real protocol (JSON and multipart bodies, long polling,
file download) for python-telegram-bot to run unmodified against it, and records
every call so a test can assert on what the bot actually sent.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

TOKEN = "123456:TEST-TOKEN"
BOT = {
    "id": 123456,
    "is_bot": True,
    "first_name": "SynthBot",
    "username": "synth_test_bot",
    "can_join_groups": True,
    "can_read_all_group_messages": False,
    "supports_inline_queries": False,
}


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    files: dict[str, bytes] = field(default_factory=dict)


class FakeTelegram:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.calls: list[Call] = []
        self.updates: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}  # file_id -> content
        self.chats: dict[int, dict[str, Any]] = {}
        self.member_count = 3
        self._failures: dict[str, list[dict[str, Any]]] = {}
        self._wake = asyncio.Event()
        self._next_message_id = 1000
        self._runner: web.AppRunner | None = None
        self.url = ""

    # --- scripting -------------------------------------------------------------

    def push_update(self, message: dict[str, Any]) -> None:
        update_id = len(self.updates) + 1
        self.updates.append({"update_id": update_id, "message": message})
        self._wake.set()

    def fail(
        self,
        method: str,
        description: str,
        code: int = 400,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        """Make the next call to ``method`` fail with a Telegram error."""
        self._failures.setdefault(method, []).append(
            {
                "ok": False,
                "error_code": code,
                "description": description,
                **({"parameters": parameters} if parameters else {}),
            }
        )

    def sent(self, *methods: str) -> list[Call]:
        return [
            c for c in self.calls if c.method in methods and not c.params.get("_failed")
        ]

    # --- server ----------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/bot{token}/{method}", self._api)
        app.router.add_get("/file/bot{token}/{path:.*}", self._file)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]  # noqa: SLF001
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _read_params(
        self, request: web.Request
    ) -> tuple[dict[str, Any], dict[str, bytes]]:
        """Form and multipart bodies (what python-telegram-bot sends) or JSON.

        Form values are strings on the wire; structured ones (numbers, lists,
        objects) are decoded back so a test can compare them naturally. ``text`` and
        ``caption`` are always kept verbatim.
        """
        params: dict[str, Any] = {}
        files: dict[str, bytes] = {}
        if request.content_type == "application/json":
            raw = await request.read()
            return (json.loads(raw) if raw else {}), files
        form = await request.post()
        for key, value in form.items():
            if isinstance(value, web.FileField):
                files[key] = value.file.read()
                continue
            text = str(value)
            if key not in ("text", "caption"):
                try:
                    text = json.loads(text)
                except ValueError:
                    pass
            params[key] = text
        return params, files

    async def _api(self, request: web.Request) -> web.Response:
        if request.match_info["token"] != self.token:
            return web.json_response(
                {"ok": False, "error_code": 401, "description": "Unauthorized"},
                status=401,
            )
        method = request.match_info["method"]
        params, files = await self._read_params(request)
        queue = self._failures.get(method)
        if queue:
            failure = queue.pop(0)
            self.calls.append(Call(method, {**params, "_failed": True}, files))
            return web.json_response(failure, status=failure["error_code"])
        if method != "getUpdates":
            self.calls.append(Call(method, params, files))
        else:
            return await self._get_updates(params)
        return web.json_response({"ok": True, "result": self._result(method, params)})

    async def _get_updates(self, params: dict[str, Any]) -> web.Response:
        offset = int(params.get("offset") or 0)
        timeout = float(params.get("timeout") or 0)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            ready = [u for u in self.updates if u["update_id"] >= offset]
            if ready or asyncio.get_running_loop().time() >= deadline:
                self.calls.append(Call("getUpdates", params))
                return web.json_response({"ok": True, "result": ready})
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    max(0.01, deadline - asyncio.get_running_loop().time()),
                )
            except asyncio.TimeoutError:
                pass

    def _result(self, method: str, params: dict[str, Any]) -> Any:
        if method == "getMe":
            return BOT
        if method == "getChat":
            chat_id = int(params["chat_id"])
            chat = self.chats.get(
                chat_id, {"id": chat_id, "type": "private", "first_name": "x"}
            )
            # The real API always sends these two; python-telegram-bot requires them.
            return {"accent_color_id": 0, "max_reaction_count": 11, **chat}
        if method == "getChatMemberCount":
            return self.member_count
        if method == "getFile":
            file_id = params["file_id"]
            if file_id not in self.files:
                return {"file_id": file_id, "file_unique_id": file_id}
            return {
                "file_id": file_id,
                "file_unique_id": file_id,
                "file_size": len(self.files[file_id]),
                "file_path": f"files/{file_id}",
            }
        if method.startswith("send"):
            self._next_message_id += 1
            chat_id = int(params["chat_id"])
            message: dict[str, Any] = {
                "message_id": self._next_message_id,
                "date": 1_700_000_000,
                "chat": {
                    "id": chat_id,
                    "type": "private" if chat_id > 0 else "supergroup",
                },
                "from": BOT,
            }
            if "text" in params:
                message["text"] = params["text"]
            return message
        return True

    async def _file(self, request: web.Request) -> web.Response:
        file_id = request.match_info["path"].removeprefix("files/")
        if file_id not in self.files:
            return web.Response(status=404)
        return web.Response(body=self.files[file_id])


def private_message(
    text: str,
    *,
    chat_id: int = 42,
    user_id: int = 42,
    name: str = "Alice",
    message_id: int = 7,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "date": 1_700_000_000,
        "chat": {
            "id": chat_id,
            "type": "private",
            "first_name": name,
            "username": name.lower(),
        },
        "from": {
            "id": user_id,
            "is_bot": False,
            "first_name": name,
            "username": name.lower(),
        },
        "text": text,
        **extra,
    }
