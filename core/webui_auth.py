"""WebUI authentication: one gate for every HTTP and WebSocket route.

The WebUI exposes administrative surfaces (config writes, agent runs, skin
management, SQL console...). Gating routes one by one left almost all of them
open, so the check lives in a single pure-ASGI middleware instead.

Authentication is OPT-IN (default: off, as before this module existed):

* ``SYNTH_WEBUI_API_TOKEN`` set -> the gate is on and that value is the token.
* ``SYNTH_WEBUI_AUTH=enabled`` without a token -> the gate is on with a random
  token generated once and persisted (mode 0600) to ``<data root>/.webui_token``.
* neither -> no authentication.

A request is authorised by any of: ``Authorization: Bearer <token>``,
``?token=<token>`` (legacy clients and the Stage frontend) or the
``synth_webui_session`` cookie set by ``POST /login`` (browsers; HttpOnly,
SameSite=Strict, so no JavaScript changes are needed for the legacy UI).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional
from urllib.parse import parse_qs, quote, urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from core import app_paths
from core.logging_utils import log_info, log_warning

LOG_PREFIX = "[WebUIAuth]"
COOKIE_NAME = "synth_webui_session"
COOKIE_MAX_AGE = 30 * 24 * 3600
TOKEN_FILE_NAME = ".webui_token"

# Reachable without credentials. The Minecraft skin PNG is fetched server-side
# by SkinRestorer/SkinsRestorer, which cannot log in.
PUBLIC_PATHS = frozenset(
    {
        "/login",
        "/logout",
        "/health",
        "/api/plugins/minecraft_vessel/skin.png",
    }
)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_FAIL_WINDOW_SEC = 60.0
_FAIL_LIMIT = 20
_FAIL_TRACKED_MAX = 4096

_cached_token: Optional[str] = None
_failures: Dict[str, Deque[float]] = {}


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------


def auth_enabled() -> bool:
    """True when ``SYNTH_WEBUI_AUTH`` explicitly asks for a generated token."""
    value = os.getenv("SYNTH_WEBUI_AUTH", "").strip().lower()
    return value in {"enabled", "enable", "on", "true", "1", "yes", "auto"}


def _token_file() -> Path:
    return app_paths.state_path(TOKEN_FILE_NAME)


def _load_or_create_token() -> str:
    global _cached_token
    if _cached_token:
        return _cached_token
    path = _token_file()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            _cached_token = existing
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        log_info(
            f"{LOG_PREFIX} Generated a WebUI access token; read it from {path} "
            "(or set SYNTH_WEBUI_API_TOKEN)."
        )
    except OSError as exc:
        log_warning(
            f"{LOG_PREFIX} Could not persist the WebUI token to {path} ({exc}); "
            "using an in-memory token that changes on restart. "
            "Set SYNTH_WEBUI_API_TOKEN to make it stable."
        )
    _cached_token = token
    return token


def configured_token() -> Optional[str]:
    """Return the active token, or ``None`` when authentication is off."""
    explicit = os.getenv("SYNTH_WEBUI_API_TOKEN", "").strip()
    if explicit:
        return explicit
    if auth_enabled():
        return _load_or_create_token()
    return None


def _session_value(token: str) -> str:
    return hmac.new(
        token.encode("utf-8"), b"synth-webui-session-v1", hashlib.sha256
    ).hexdigest()


def _eq(a: Optional[str], b: str) -> bool:
    return a is not None and hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _parse_cookies(header: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            cookies[name] = value
    return cookies


def credential_kind(
    headers: Dict[str, str], query: Dict[str, str], token: str
) -> Optional[str]:
    """Return ``"token"``, ``"cookie"`` or ``None`` for the supplied credentials."""
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and _eq(auth[7:].strip(), token):
        return "token"
    if _eq(query.get("token"), token):
        return "token"
    cookie = _parse_cookies(headers.get("cookie", "")).get(COOKIE_NAME)
    if _eq(cookie, _session_value(token)):
        return "cookie"
    return None


def _headers_dict(scope: Scope) -> Dict[str, str]:
    return {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope.get("headers", [])
    }


def _query_dict(scope: Scope) -> Dict[str, str]:
    parsed = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    return {k: v[0] for k, v in parsed.items() if v}


def request_authorized(request: Request) -> bool:
    """FastAPI dependency helper: True when open or credentials are valid."""
    token = configured_token()
    if token is None:
        return True
    return (
        credential_kind(dict(request.headers), dict(request.query_params), token)
        is not None
    )


def websocket_authorized(websocket: Any) -> bool:
    token = configured_token()
    if token is None:
        return True
    return (
        credential_kind(dict(websocket.headers), dict(websocket.query_params), token)
        is not None
    )


# ---------------------------------------------------------------------------
# Brute-force throttle
# ---------------------------------------------------------------------------


def _client_ip(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


def _throttled(ip: str) -> bool:
    bucket = _failures.get(ip)
    if not bucket:
        return False
    cutoff = time.monotonic() - _FAIL_WINDOW_SEC
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    return len(bucket) >= _FAIL_LIMIT


def _record_failure(ip: str) -> None:
    if len(_failures) >= _FAIL_TRACKED_MAX and ip not in _failures:
        _failures.clear()
    _failures.setdefault(ip, deque(maxlen=_FAIL_LIMIT * 2)).append(time.monotonic())


# ---------------------------------------------------------------------------
# Origin check (CSRF / cross-site WebSocket hijacking for cookie sessions)
# ---------------------------------------------------------------------------


def _allowed_origins() -> set[str]:
    raw = os.getenv("SYNTH_WEBUI_CORS_ORIGINS", "")
    return {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}


def origin_allowed(headers: Dict[str, str]) -> bool:
    origin = headers.get("origin")
    if not origin or origin == "null":
        return origin is None
    if origin.rstrip("/") in _allowed_origins():
        return True
    origin_host = urlparse(origin).netloc.lower()
    hosts = {headers.get("host", "").lower()}
    forwarded = headers.get("x-forwarded-host")
    if forwarded:
        hosts.add(forwarded.split(",")[0].strip().lower())
    return origin_host in hosts


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------

_LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SyntH sign in</title>
<style>
:root{color-scheme:light dark;--bg:#f5f5f7;--fg:#1c1c1e;--card:#fff;--line:#d0d0d5;--acc:#5b5bd6}
@media (prefers-color-scheme:dark){:root{--bg:#111114;--fg:#ececf1;--card:#1b1b20;--line:#34343c}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}
form{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;width:min(360px,calc(100vw - 32px));display:grid;gap:12px}
input,button{font:inherit;padding:10px 12px;border-radius:8px;border:1px solid var(--line);background:transparent;color:inherit}
button{background:var(--acc);color:#fff;border-color:var(--acc);cursor:pointer}
.err{color:#d33;font-size:14px;margin:0}
.hint{font-size:13px;opacity:.7;margin:0}
</style></head><body>
<form method="post" action="/login">
<h1 style="margin:0;font-size:20px">Synthetic Heart</h1>
<input type="password" name="token" placeholder="Access token" autocomplete="current-password" autofocus required>
<input type="hidden" name="next" value="__NEXT__">
__ERROR__
<button type="submit">Sign in</button>
<p class="hint">The token is in <code>/config/.webui_token</code> or <code>SYNTH_WEBUI_API_TOKEN</code>.</p>
</form></body></html>"""


def _safe_next(value: Optional[str]) -> str:
    if (
        value
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
    ):
        return value
    return "/"


def _login_page(next_path: str, error: str = "", status: int = 200) -> HTMLResponse:
    from html import escape

    err = f'<p class="err">{escape(error)}</p>' if error else ""
    html = _LOGIN_HTML.replace("__NEXT__", escape(next_path, quote=True)).replace(
        "__ERROR__", err
    )
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store"})


def _session_cookie_header(token: str, secure: bool) -> str:
    parts = [
        f"{COOKIE_NAME}={_session_value(token)}",
        "Path=/",
        f"Max-Age={COOKIE_MAX_AGE}",
        "HttpOnly",
        "SameSite=Strict",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _is_secure(scope: Scope, headers: Dict[str, str]) -> bool:
    return scope.get("scheme") in {"https", "wss"} or headers.get(
        "x-forwarded-proto", ""
    ).startswith("https")


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class WebUIAuthMiddleware:
    """Pure-ASGI gate in front of every HTTP and WebSocket route."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = configured_token()
        if token is None:
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        headers = _headers_dict(scope)
        ip = _client_ip(scope)

        # CORS preflights carry no credentials and have no handlers of their own.
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http" and path in ("/login", "/logout"):
            response = await self._login_logout(scope, receive, headers, token, ip)
            await response(scope, receive, send)
            return

        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if _throttled(ip):
            await self._reject(scope, receive, send, 429, "Too many failed attempts")
            return

        kind = credential_kind(headers, _query_dict(scope), token)
        if kind is None:
            if headers.get("authorization") or "token" in _query_dict(scope):
                _record_failure(ip)
            await self._unauthorized(scope, receive, send, headers)
            return

        needs_origin = (
            scope["type"] == "websocket" or scope.get("method") not in _SAFE_METHODS
        )
        if kind == "cookie" and needs_origin and not origin_allowed(headers):
            await self._reject(
                scope, receive, send, 403, "Cross-origin request blocked"
            )
            return

        await self.app(scope, receive, send)

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, status: int, detail: str
    ) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": detail})
            return
        await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)

    async def _unauthorized(
        self, scope: Scope, receive: Receive, send: Send, headers: Dict[str, str]
    ) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        path = scope.get("path", "")
        wants_html = (
            scope.get("method") == "GET"
            and "text/html" in headers.get("accept", "")
            and not path.startswith("/api/")
        )
        if wants_html:
            query = scope.get("query_string", b"").decode("latin-1")
            target = path + (f"?{query}" if query else "")
            await RedirectResponse(f"/login?next={quote(target)}", status_code=303)(
                scope, receive, send
            )
            return
        await JSONResponse({"detail": "Invalid or missing API token"}, status_code=401)(
            scope, receive, send
        )

    async def _login_logout(
        self,
        scope: Scope,
        receive: Receive,
        headers: Dict[str, str],
        token: str,
        ip: str,
    ) -> Response:
        request = Request(scope, receive)
        secure = _is_secure(scope, headers)
        if scope["path"] == "/logout":
            response: Response = RedirectResponse("/login", status_code=303)
            response.delete_cookie(COOKIE_NAME, path="/")
            return response

        if request.method == "GET":
            return _login_page(_safe_next(request.query_params.get("next")))

        if request.method != "POST":
            return JSONResponse({"detail": "Method not allowed"}, status_code=405)
        if not origin_allowed(headers):
            return JSONResponse(
                {"detail": "Cross-origin request blocked"}, status_code=403
            )
        if _throttled(ip):
            return JSONResponse({"detail": "Too many failed attempts"}, status_code=429)

        body = await request.body()
        is_json = "application/json" in headers.get("content-type", "")
        supplied: Optional[str] = None
        next_path = "/"
        try:
            if is_json:
                data = json.loads(body or b"{}")
                supplied = (
                    str(data.get("token", "")) if isinstance(data, dict) else None
                )
            else:
                form = parse_qs(body.decode("utf-8", "replace"))
                supplied = (form.get("token") or [None])[0]
                next_path = _safe_next((form.get("next") or [None])[0])
        except ValueError:
            supplied = None

        if not _eq(supplied, token):
            _record_failure(ip)
            if is_json:
                return JSONResponse({"detail": "Invalid token"}, status_code=401)
            return _login_page(next_path, "Invalid token", status=401)

        if is_json:
            response = JSONResponse({"status": "ok"})
        else:
            response = RedirectResponse(next_path, status_code=303)
        response.headers.append("set-cookie", _session_cookie_header(token, secure))
        return response
