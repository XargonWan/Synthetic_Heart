"""Web content an extension adds to the Core's web server.

Two things, both declared in the manifest (``[provides.web]``) and both needing a
capability that is declared **and** granted:

``web.static``
    a directory of the package, served read-only at ``/x/<id>/`` (and at
    ``/<alias>/``). Files only: no listing, nothing outside the directory (a path with
    ``..`` or a symlink that leaves it is refused).
``web.routes``
    HTTP requests under ``/x/<id>/api/`` are forwarded to the extension as the call
    ``web.request`` and its answer is sent back. The extension never opens a port, so
    it cannot be reached around the WebUI's own token check.

``web.ws``
    WebSocket connections to ``/x/<id>/ws/<path>`` (behind the WebUI token) are
    handed to the extension: ``web.ws.open`` (it may refuse), ``web.ws.message`` for
    each frame, ``web.ws.closed``; it answers with ``web.ws.send`` and ``web.ws.close``.

This module is the logic; :mod:`core.extensions.webui_web` is the thin FastAPI layer.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.extensions.gateway import ApiMethod, CallContext, Gateway, Param
from synth_sdk.rpc import ErrorCode, RpcError

CAP_STATIC = "web.static"
CAP_ROUTES = "web.routes"
CAP_WS = "web.ws"
#: Run the pages with the Core's own origin (see ``WebProvision.same_origin``).
CAP_ORIGIN = "web.origin"
#: Sent with every static file of an extension that does not have ``web.origin``: the
#: page gets an opaque origin, so it cannot read the WebUI session (token, storage) or
#: act as the person.
SANDBOX_CSP = "sandbox allow-scripts allow-forms allow-popups allow-downloads"
#: Most WebSocket connections one extension may hold at a time.
MAX_WS_PER_EXTENSION = 16

#: Largest request or response body forwarded (bytes); RPC messages are 1 MiB and
#: the body travels as base64.
MAX_BODY = 600_000
#: Largest static file served.
MAX_STATIC_FILE = 25 * 1024 * 1024
#: How long the extension has to answer a forwarded request.
REQUEST_TIMEOUT = 60.0
#: Methods that may be forwarded.
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
#: Request headers the extension is told about. Credentials never are.
FORWARD_HEADERS = frozenset(
    {"content-type", "accept", "accept-language", "user-agent", "x-requested-with"}
)
#: Response headers an extension may set.
ALLOWED_RESPONSE_HEADERS = frozenset(
    {"content-type", "cache-control", "content-language", "etag", "location"}
)
MAX_HEADERS = 16


@dataclass(frozen=True)
class WebTarget:
    """A running extension that serves web content."""

    extension_id: str
    root: Path
    static: bool
    routes: bool
    websockets: bool = False
    same_origin: bool = False


def resolve_static(root: Path, relative: str) -> Path | None:
    """The file ``relative`` names inside ``root`` (``index.html`` for a directory),
    or ``None`` when it does not exist or would leave ``root``."""
    parts = [p for p in relative.replace("\\", "/").split("/") if p]
    if any(p in (".", "..") or "\x00" in p for p in parts):
        return None
    try:
        base = root.resolve()
        target = base.joinpath(*parts).resolve()
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file() or target.stat().st_size > MAX_STATIC_FILE:
            return None
        # After following every symlink the file must still be inside the directory.
        if base not in target.resolve().parents:
            return None
    except (OSError, ValueError):
        return None
    return target


def media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def request_params(
    method: str,
    path: str,
    query: Mapping[str, str],
    headers: Mapping[str, str],
    body: bytes,
) -> dict[str, Any]:
    """What the extension is told about a request."""
    return {
        "method": method,
        "path": path,
        "query": dict(query),
        "headers": {
            k.lower(): v for k, v in headers.items() if k.lower() in FORWARD_HEADERS
        },
        "body": base64.b64encode(body).decode("ascii"),
    }


def clean_response(raw: Any) -> tuple[int, dict[str, str], bytes]:
    """The extension's answer as ``(status, headers, body)``; raises ``ValueError`` when
    it is not an acceptable answer."""
    if not isinstance(raw, dict):
        raise ValueError("the extension did not answer with an object")
    status = raw.get("status", 200)
    if (
        not isinstance(status, int)
        or isinstance(status, bool)
        or not 200 <= status <= 599
    ):
        raise ValueError("status must be an integer from 200 to 599")
    headers: dict[str, str] = {}
    given = raw.get("headers", {})
    if not isinstance(given, dict) or len(given) > MAX_HEADERS:
        raise ValueError("headers must be an object of at most 16 entries")
    for key, value in given.items():
        name = str(key).lower()
        if name not in ALLOWED_RESPONSE_HEADERS:
            continue  # cookies and the like are the Core's, not the extension's
        text = str(value)
        if "\r" in text or "\n" in text or len(text) > 2048:
            raise ValueError(f"header {name!r} is not acceptable")
        headers[name] = text
    encoded = raw.get("body", "")
    if not isinstance(encoded, str):
        raise ValueError("body must be a base64 string")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("body is not valid base64") from None
    if len(body) > MAX_BODY:
        raise ValueError("body is too large")
    return status, headers, body


def target_for(
    gateway: Gateway, extension_id: str, install_dir: Path
) -> WebTarget | None:
    """The web content of ``extension_id`` if it is running, declares web content and
    has been granted the matching capabilities."""
    session = gateway.sessions.get(extension_id)
    if session is None:
        return None
    web = session.manifest.provides.web
    if web is None:
        return None
    static = bool(web.static) and session.allowed(CAP_STATIC)
    routes = web.routes and session.allowed(CAP_ROUTES)
    websockets = web.websockets and session.allowed(CAP_WS)
    if not (static or routes or websockets):
        return None
    return WebTarget(
        extension_id,
        install_dir / web.static if web.static else install_dir,
        static,
        routes,
        websockets,
        web.same_origin and session.allowed(CAP_ORIGIN),
    )


def alias_owner(gateway: Gateway, alias: str) -> str | None:
    """The running extension that serves ``/<alias>/`` (the first, if two declare it)."""
    for extension_id in sorted(gateway.sessions):
        session = gateway.sessions[extension_id]
        web = session.manifest.provides.web
        if (
            web is not None
            and web.alias == alias
            and web.static
            and session.allowed(CAP_STATIC)
        ):
            return extension_id
    return None


async def forward(
    gateway: Gateway, extension_id: str, params: dict[str, Any]
) -> tuple[int, dict[str, str], bytes]:
    """Send the request to the extension and return its cleaned answer. Raises
    ``LookupError`` if it is not running, ``TimeoutError`` if it is too slow and
    ``ValueError`` for an unusable answer."""
    session = gateway.sessions.get(extension_id)
    if session is None:
        raise LookupError(extension_id)
    try:
        raw = await session.call("web.request", params, timeout=REQUEST_TIMEOUT)
    except RpcError as exc:
        if exc.code == ErrorCode.TIMEOUT:
            raise TimeoutError(extension_id) from None
        raise ValueError(f"the extension failed: {exc.message[:120]}") from None
    return clean_response(raw)


# ----------------------------------------------------------------- WebSockets


@dataclass
class WsConnection:
    extension_id: str
    send_text: Any  # async callable(str)
    send_bytes: Any  # async callable(bytes)
    close: Any  # async callable(int)


class WebSocketHub:
    """The WebSocket connections handed to extensions, by connection id."""

    def __init__(self) -> None:
        self.connections: dict[str, WsConnection] = {}
        self._closing: set[asyncio.Task[None]] = set()

    def close_soon(self, connection: WsConnection, code: int) -> None:
        """Close without waiting: the closing handshake needs the peer to read, and the
        extension's call must not hang on a slow or gone client."""

        async def run() -> None:
            try:
                await connection.close(code)
            except Exception:  # noqa: BLE001 - the socket may be gone already
                pass

        task = asyncio.ensure_future(run())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def count(self, extension_id: str) -> int:
        return sum(
            1 for c in self.connections.values() if c.extension_id == extension_id
        )

    def add(self, connection: WsConnection) -> str | None:
        """Register a connection; ``None`` if the extension holds too many."""
        import secrets

        if self.count(connection.extension_id) >= MAX_WS_PER_EXTENSION:
            return None
        conn_id = secrets.token_hex(8)
        self.connections[conn_id] = connection
        return conn_id

    def remove(self, conn_id: str) -> None:
        self.connections.pop(conn_id, None)

    async def drop_extension(self, extension_id: str) -> None:
        """Close every connection of an extension that went away."""
        for conn_id, connection in list(self.connections.items()):
            if connection.extension_id == extension_id:
                self.connections.pop(conn_id, None)
                self.close_soon(connection, 1012)


def register_web_api(gateway: Gateway, hub: WebSocketHub) -> None:
    """Register ``web.ws.send`` and ``web.ws.close`` on the gateway."""
    from synth_sdk.rpc import ErrorCode as Code

    def owned(ctx: CallContext, conn_id: str) -> WsConnection:
        connection = hub.connections.get(conn_id)
        if connection is None or connection.extension_id != ctx.extension_id:
            raise RpcError(Code.INVALID_PARAMS, "unknown connection")
        return connection

    async def send(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        connection = owned(ctx, args["conn_id"])
        text, data = args["text"], args["data"]
        if (text is None) == (data is None):
            raise RpcError(Code.INVALID_PARAMS, "send exactly one of 'text' or 'data'")
        try:
            if text is not None:
                await connection.send_text(text)
            else:
                try:
                    raw = base64.b64decode(data, validate=True)
                except (binascii.Error, ValueError):
                    raise RpcError(Code.INVALID_PARAMS, "data is not base64") from None
                if len(raw) > MAX_BODY:
                    raise RpcError(Code.INVALID_PARAMS, "data is too large")
                await connection.send_bytes(raw)
        except RpcError:
            raise
        except Exception:  # noqa: BLE001 - the peer is gone
            hub.remove(args["conn_id"])
            return {"ok": False}
        return {"ok": True}

    async def close(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        connection = owned(ctx, args["conn_id"])
        hub.remove(args["conn_id"])
        hub.close_soon(connection, int(args["code"]))
        return {"ok": True}

    gateway.register(
        ApiMethod(
            "web.ws.send",
            CAP_WS,
            (
                Param("conn_id", "str", max_length=64),
                Param("text", "str", required=False, default=None, max_length=MAX_BODY),
                Param("data", "str", required=False, default=None),
            ),
            send,
            "Send a text frame (text) or a binary frame (data, base64) on a connection.",
        )
    )
    gateway.register(
        ApiMethod(
            "web.ws.close",
            CAP_WS,
            (
                Param("conn_id", "str", max_length=64),
                Param(
                    "code",
                    "int",
                    required=False,
                    default=1000,
                    minimum=1000,
                    maximum=4999,
                ),
            ),
            close,
            "Close a connection.",
        )
    )
