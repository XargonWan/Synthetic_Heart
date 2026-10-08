# interface/telegram/webui_login.py
"""WebUI routes + panel script for the Telegram user-account login.

Registered through the plugin extension API of the WebUI
(``register_plugin_api_route`` / ``register_plugin_js``).  Those routes bypass
the FastAPI ``Depends`` machinery, so the optional ``SYNTH_WEBUI_API_TOKEN``
bearer check is repeated here.  Request bodies carry the phone code and the
2FA password: they are never logged.
"""

import asyncio
from pathlib import Path
from typing import Any, Optional

from core.config_manager import config_registry
from core.logging_utils import log_info, log_warning

_JS_PATH = Path(__file__).with_name("login_panel.js")
_ROUTE_PREFIX = "/api/telegram/login"
_registered = False
_task: Optional[asyncio.Task] = None


def _unauthorized() -> Any:
    from starlette.responses import JSONResponse

    return JSONResponse({"detail": "Invalid or missing API token"}, status_code=401)


def _authorized(request: Any) -> bool:
    from core.karada_api import _configured_api_token, _token_from_request

    expected = _configured_api_token()
    return expected is None or _token_from_request(request) == expected


async def _body(request: Any) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _status() -> dict:
    from interface.telegram.telegram import login_status

    return login_status()


async def _route_status(request: Any) -> Any:
    return _status() if _authorized(request) else _unauthorized()


async def _route_send_code(request: Any) -> Any:
    from interface.telegram._login import FLOW
    from interface.telegram.telegram import _cfg

    if not _authorized(request):
        return _unauthorized()
    phone = str((await _body(request)).get("phone") or _cfg("TELEGRAM_PHONE")).strip()
    if not phone.startswith("+") or not phone[1:].replace(" ", "").isdigit():
        return {
            **_status(),
            "login": {
                **FLOW.snapshot(),
                "error": "Use the international format, e.g. +391234567890.",
            },
        }
    try:
        api_id = int(_cfg("TELEGRAM_API_ID"))
    except ValueError:
        api_id = 0
    api_hash = _cfg("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        return {
            **_status(),
            "login": {
                **FLOW.snapshot(),
                "error": "Set TELEGRAM_API_ID and TELEGRAM_API_HASH first, then reload the interface.",
            },
        }
    await config_registry.set_value("TELEGRAM_PHONE", phone)
    await FLOW.send_code(api_id, api_hash, phone.replace(" ", ""))
    return _status()


async def _route_verify_code(request: Any) -> Any:
    from interface.telegram._login import FLOW

    if not _authorized(request):
        return _unauthorized()
    await FLOW.verify_code(str((await _body(request)).get("code") or ""))
    return _status()


async def _route_verify_password(request: Any) -> Any:
    from interface.telegram._login import FLOW

    if not _authorized(request):
        return _unauthorized()
    await FLOW.verify_password(str((await _body(request)).get("password") or ""))
    return _status()


async def _route_cancel(request: Any) -> Any:
    from interface.telegram._login import FLOW

    if not _authorized(request):
        return _unauthorized()
    await FLOW.cancel()
    return _status()


async def _route_logout(request: Any) -> Any:
    from interface.telegram.telegram import logout_account

    if not _authorized(request):
        return _unauthorized()
    await logout_account()
    return _status()


def _register_now() -> bool:
    global _registered
    try:
        from core.webui import synth_webui_interface
    except Exception:
        return False
    if synth_webui_interface is None:
        return False
    routes = {
        "status": _route_status,
        "send_code": _route_send_code,
        "verify_code": _route_verify_code,
        "verify_password": _route_verify_password,
        "cancel": _route_cancel,
        "logout": _route_logout,
    }
    for name, handler in routes.items():
        synth_webui_interface.register_plugin_api_route(
            f"{_ROUTE_PREFIX}/{name}", handler
        )
    synth_webui_interface.register_plugin_js(
        "telegram_login", _JS_PATH.read_text(encoding="utf-8")
    )
    _registered = True
    log_info("[telegram] WebUI login panel registered")
    return True


async def _register_when_ready() -> None:
    global _task
    try:
        for _ in range(60):
            if _register_now():
                return
            await asyncio.sleep(2)
        log_warning("[telegram] WebUI not available; login panel not registered")
    finally:
        _task = None


def schedule_registration() -> None:
    """Register now if the WebUI exists, otherwise retry in the background."""
    global _task
    if _register_now():
        return
    if _task is not None and not _task.done():
        return
    try:
        _task = asyncio.get_running_loop().create_task(_register_when_ready())
    except RuntimeError:
        pass
