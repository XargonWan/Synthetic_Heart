"""OpenAI-compatible HTTP API extension.

Exposes the SyntH as one model on ``/v1/models``, ``/v1/chat/completions`` and
``/v1/completions`` (plain JSON or standard SSE). Every request is a turn through the
Core's message chain: the extension hands the question to ``interfaces.inbound`` with
``wait_reply`` and answers the HTTP request with the text that comes back. A turn the
agent lane runs detached answers later through a normal outbound delivery, which this
module matches to the request that is waiting for it. Imports nothing from the Core.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any

import convert
from aiohttp import web

from synth_sdk.runtime import Context

#: Largest request body accepted.
MAX_BODY_BYTES = 4 * 1024 * 1024
#: A streaming response is kept alive with a comment this often while the turn runs.
KEEPALIVE_SECONDS = 15.0

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class Server:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.token = ""
        self.max_history = 20
        self.timeout = 0.0
        self.synth_name = "SyntH"
        self.capabilities: dict[str, bool] = {}
        #: Conversations waiting for a delivery: chat id -> future of the reply text.
        self.waiting: dict[str, asyncio.Future[str]] = {}

    # ----------------------------------------------------------------- setup

    async def configure(self) -> str:
        config = self.ctx.config
        self.token = str(await config.get("OPENAI_API_TOKEN") or "").strip()
        self.max_history = max(0, int(await config.get("OPENAI_API_MAX_HISTORY") or 0))
        self.timeout = max(0.0, float(await config.get("OPENAI_API_TIMEOUT") or 0))
        info = await self.ctx.core.get()
        self.synth_name = str(info.get("synth_name") or "").strip() or self.synth_name
        self.capabilities = dict(info.get("capabilities") or {})
        host = str(await config.get("OPENAI_API_HOST") or "").strip()
        return host or ("0.0.0.0" if info.get("in_container") else "127.0.0.1")

    def app(self) -> web.Application:
        app = web.Application(
            client_max_size=MAX_BODY_BYTES, middlewares=[self.errors, self.auth]
        )
        for prefix in ("", "/v1"):
            app.router.add_get(prefix + "/", self.index)
        app.router.add_get("/v1/models", self.models)
        app.router.add_get("/v1/models/{model}", self.model)
        app.router.add_post("/v1/chat/completions", self.chat)
        app.router.add_post("/v1/completions", self.completions)
        return app

    # ------------------------------------------------------------ middleware

    @web.middleware
    async def errors(
        self, request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        try:
            return await handler(request)
        except convert.BadRequest as exc:
            return self.fail(400, str(exc), "invalid_request_error")
        except web.HTTPRequestEntityTooLarge:
            return self.fail(413, "request body is too large", "invalid_request_error")
        except web.HTTPNotFound:
            return self.fail(404, "not found", "invalid_request_error")
        except web.HTTPMethodNotAllowed:
            return self.fail(405, "method not allowed", "invalid_request_error")
        except web.HTTPException as exc:
            return self.fail(exc.status, exc.reason or "error", "invalid_request_error")
        except Exception as exc:  # noqa: BLE001
            self.ctx.stats.incr("errors", kind="request")
            self.ctx.log.error(f"{request.path}: {type(exc).__name__}: {exc}")
            return self.fail(500, "internal error", "server_error")

    @web.middleware
    async def auth(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        if self.token and request.path.rstrip("/") not in ("", "/v1"):
            given = request.headers.get("Authorization", "")
            scheme, _, value = given.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                value.strip().encode(), self.token.encode()
            ):
                return self.fail(
                    401,
                    "invalid or missing API key",
                    "invalid_request_error",
                    "invalid_api_key",
                )
        return await handler(request)

    @staticmethod
    def fail(
        status: int, message: str, kind: str, code: str | None = None
    ) -> web.Response:
        return web.json_response(convert.error_body(message, kind, code), status=status)

    # ---------------------------------------------------------------- routes

    async def index(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "interface": convert.INTERFACE_ID})

    async def models(self, request: web.Request) -> web.Response:
        return web.json_response(
            convert.models_list(self.synth_name, self.capabilities)
        )

    async def model(self, request: web.Request) -> web.Response:
        if request.match_info["model"] != self.synth_name:
            return self.fail(
                404,
                f"the model {request.match_info['model']!r} does not exist",
                "invalid_request_error",
                "model_not_found",
            )
        return web.json_response(
            convert.model_descriptor(self.synth_name, self.capabilities)
        )

    async def body(self, request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise convert.BadRequest("the body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise convert.BadRequest("the body must be a JSON object")
        return data

    async def chat(self, request: web.Request) -> web.StreamResponse:
        data = await self.body(request)
        question, history = convert.split_messages(
            data.get("messages"), self.max_history
        )
        return await self.serve(request, data, question, history, chat=True)

    async def completions(self, request: web.Request) -> web.StreamResponse:
        data = await self.body(request)
        question = convert.prompt_text(data.get("prompt"))
        return await self.serve(request, data, question, [], chat=False)

    # ------------------------------------------------------------------ turn

    async def serve(
        self,
        request: web.Request,
        data: dict[str, Any],
        question: str,
        history: list[dict[str, str]],
        *,
        chat: bool,
    ) -> web.StreamResponse:
        stream = data.get("stream") is True
        model = str(data.get("model") or self.synth_name)
        chat_id = convert.chat_id_for(convert.conversation_id(data))
        if chat_id in self.waiting:
            return self.fail(
                409,
                "this conversation is still answering a previous request",
                "invalid_request_error",
                "conversation_busy",
            )
        request_id = convert.new_id("chatcmpl" if chat else "cmpl")
        created = convert.now()
        turn = asyncio.ensure_future(self.turn(chat_id, question, history))
        self.ctx.stats.incr("requests", stream="yes" if stream else "no")
        if not stream:
            text = await turn
            body = (
                convert.chat_completion(request_id, model, text)
                if chat
                else convert.text_completion(request_id, model, text)
            )
            return web.json_response(body)
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        try:
            if chat:
                await response.write(
                    convert.sse(
                        convert.chat_chunk(
                            request_id,
                            model,
                            created=created,
                            delta={"role": "assistant", "content": ""},
                        )
                    )
                )
            while not turn.done():
                await asyncio.wait({turn}, timeout=KEEPALIVE_SECONDS)
                if not turn.done():
                    await response.write(convert.SSE_KEEPALIVE)
            text = turn.result()
            for piece in convert.split_for_stream(text):
                await response.write(
                    convert.sse(
                        convert.chat_chunk(
                            request_id, model, created=created, delta={"content": piece}
                        )
                        if chat
                        else convert.text_chunk(
                            request_id, model, created=created, text=piece
                        )
                    )
                )
            await response.write(
                convert.sse(
                    convert.chat_chunk(
                        request_id, model, created=created, delta={}, finish="stop"
                    )
                    if chat
                    else convert.text_chunk(
                        request_id, model, created=created, text="", finish="stop"
                    )
                )
            )
            await response.write(convert.SSE_DONE)
        except (ConnectionResetError, asyncio.CancelledError):
            # The client went away: the turn still finishes inside the Core.
            self.ctx.stats.incr("errors", kind="client_gone")
            raise
        finally:
            if not turn.done():
                turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await turn
        return response

    async def turn(
        self, chat_id: str, question: str, history: list[dict[str, str]]
    ) -> str:
        """Run the turn and return the text that answers it ("" when it has none)."""
        path = convert.path_for(chat_id)
        loop = asyncio.get_running_loop()
        delivery: asyncio.Future[str] = loop.create_future()
        self.waiting[chat_id] = delivery
        try:
            result = await self.ctx.interfaces.inbound(
                convert.INTERFACE_ID,
                path,
                text=question,
                sender_id=f"api-user-{chat_id}",
                sender_name="API user",
                chat_id=chat_id,
                chat_type="private",
                chat_title="OpenAI API",
                skip_mention_check=True,
                wait_reply=True,
                history=history or None,
                reply_timeout=self.timeout,
            )
            reply = result.get("reply")
            if isinstance(reply, str) and reply:
                return reply
            if result.get("pending"):
                # The agent lane works detached: its text arrives as a delivery.
                limit = self.timeout or 900.0
                try:
                    return await asyncio.wait_for(delivery, timeout=limit)
                except asyncio.TimeoutError:
                    self.ctx.log.warning(f"{path}: the agent turn gave no text in time")
            return ""
        finally:
            self.waiting.pop(chat_id, None)

    # -------------------------------------------------------------- outbound

    async def send(
        self, interface_id: str, payload: dict[str, Any], origin: dict[str, Any]
    ) -> dict[str, Any]:
        chat = convert.chat_of(payload.get("interface_path")) or convert.chat_of(
            origin.get("interface_path")
        )
        text = str(payload.get("text") or "")
        waiter = self.waiting.get(chat) if chat else None
        if waiter is None or waiter.done():
            return {"ok": False, "error": "no request is waiting for this conversation"}
        if not text.strip():
            return {"ok": False, "error": "nothing to send"}
        waiter.set_result(text)
        self.ctx.stats.incr("outbound")
        return {"ok": True}


async def main(ctx: Context) -> None:
    server = Server(ctx)
    host = await server.configure()
    port = int(await ctx.config.get("OPENAI_API_PORT") or 11435)
    ctx.interfaces.on_send_message(server.send)
    await ctx.interfaces.register(
        convert.INTERFACE_ID,
        display_name="OpenAI API",
        path_hint="openai_api/<conversation>",
        description=(
            "An OpenAI-compatible HTTP API. Each conversation is a path "
            "openai_api/<conversation>; an answer is delivered to the request that "
            "is waiting for it. Text only."
        ),
    )
    runner = web.AppRunner(server.app(), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except OSError as exc:
        ctx.log.error(f"cannot listen on {host}:{port}: {exc}")
        await ctx.interfaces.set_state(
            convert.INTERFACE_ID, False, f"port {port} is not available"
        )
        await runner.cleanup()
        await ctx.stopping.wait()
        return
    ctx.log.info(f"listening on {host}:{port}")
    try:
        await ctx.stopping.wait()
    finally:
        for waiter in list(server.waiting.values()):
            waiter.cancel()
        await runner.cleanup()
