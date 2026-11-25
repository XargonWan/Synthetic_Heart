"""Serve extension setup panels in the WebUI (see :mod:`core.extensions.panels`).

Two routes, both behind the WebUI token:

``GET  /api/extensions/panels/{interface_id}``
    the panels attached to an interface, as drawn data;
``POST /api/extensions/panels/{interface_id}/{panel}/action``
    the person pressed a button or sent a form: ``{"action": ..., "values": {...}}``.

The page script (``res/synth_webui/js/extension_panels.js``) is the Core's own: it
builds the panel with DOM calls and ``textContent`` only, so nothing an extension sends
is ever read as HTML or script.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.extensions.gateway import Gateway
from core.extensions.panels import PanelStore, clean_values, run_action

ROUTE = "/api/extensions/panels"
SCRIPT_NAME = "extension_panels"
SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "res"
    / "synth_webui"
    / "js"
    / "extension_panels.js"
)


def build_router(gateway: Gateway, store: PanelStore) -> Any:
    """The FastAPI router for the two routes."""
    from core import webui_auth

    router = APIRouter()

    def unauthorized() -> JSONResponse:
        return JSONResponse({"detail": "Invalid or missing API token"}, status_code=401)

    @router.get(f"{ROUTE}/{{interface_id}}")
    async def list_panels(interface_id: str, request: Request) -> Any:
        if not webui_auth.request_authorized(request):
            return unauthorized()
        return {"panels": store.for_interface(interface_id)}

    @router.post(f"{ROUTE}/{{interface_id}}/{{panel}}/action")
    async def panel_action(interface_id: str, panel: str, request: Request) -> Any:
        if not webui_auth.request_authorized(request):
            return unauthorized()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a bad body is simply empty
            body = {}
        if not isinstance(body, dict):
            body = {}
        action = str(body.get("action") or "")
        owners = [
            extension_id
            for (extension_id, iface), panels in store.states.items()
            if iface == interface_id and panel in panels
        ]
        if len(owners) != 1 or not action:
            return JSONResponse(
                {"ok": False, "error": "unknown panel"}, status_code=404
            )
        return await run_action(
            gateway,
            owners[0],
            interface_id,
            panel,
            action,
            clean_values(body.get("values")),
        )

    return router


def mount(gateway: Gateway, store: PanelStore) -> bool:
    """Mount the routes and the page script on the running WebUI. ``False`` if it is
    not up yet."""
    try:
        from core.webui import synth_webui_interface
    except Exception:  # noqa: BLE001
        return False
    if synth_webui_interface is None:
        return False
    synth_webui_interface.register_plugin_router(
        build_router(gateway, store), "extension panels"
    )
    synth_webui_interface.register_plugin_js(
        SCRIPT_NAME, SCRIPT_PATH.read_text(encoding="utf-8")
    )
    return True


async def mount_when_ready(
    gateway: Gateway, store: PanelStore, tries: int = 60
) -> bool:
    """Retry :func:`mount` while the WebUI starts up."""
    for _ in range(tries):
        if mount(gateway, store):
            return True
        await asyncio.sleep(2)
    return False
