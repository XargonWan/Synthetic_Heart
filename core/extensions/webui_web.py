"""Serve extension web content from the WebUI's server (see :mod:`core.extensions.web`).

``GET /x/{publisher}/{name}/{path}``       static files of an extension (public: assets only)
``ANY /x/{publisher}/{name}/api/{path}``   forwarded to the extension, behind the WebUI token
``GET /{alias}/{path}``                    the static files of the extension that declared that alias

The alias routes are registered last, so every route of the Core wins over them; an
unknown alias is a plain 404.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from core.extensions import ui, web
from core.extensions.gateway import Gateway

NOT_FOUND = {"detail": "Not found"}
SCRIPT_NAME = "extension_slots"
SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "res"
    / "synth_webui"
    / "js"
    / "extension_slots.js"
)


def build_router(
    gateway: Gateway,
    install_dir: Callable[[str], Path],
    hub: web.WebSocketHub | None = None,
) -> Any:
    from core import webui_auth

    router = APIRouter()

    def static_response(extension_id: str, relative: str) -> Response:
        target = web.target_for(gateway, extension_id, install_dir(extension_id))
        if target is None or not target.static:
            return JSONResponse(NOT_FOUND, status_code=404)
        found = web.resolve_static(target.root, relative)
        if found is None:
            return JSONResponse(NOT_FOUND, status_code=404)
        headers = {"X-Content-Type-Options": "nosniff"}
        if not target.same_origin:
            headers["Content-Security-Policy"] = web.SANDBOX_CSP
        return FileResponse(found, media_type=web.media_type(found), headers=headers)

    @router.get("/api/extensions/ui")
    async def ui_entries(request: Request) -> Response:
        if not webui_auth.request_authorized(request):
            return JSONResponse(
                {"detail": "Invalid or missing API token"}, status_code=401
            )
        return JSONResponse({"entries": ui.entries(gateway)})

    @router.get("/x/{publisher}/{name}")
    @router.get("/x/{publisher}/{name}/")
    async def extension_index(publisher: str, name: str) -> Response:
        return static_response(f"{publisher}/{name}", "")

    @router.api_route(
        "/x/{publisher}/{name}/api/{path:path}",
        methods=sorted(web.METHODS),
    )
    async def extension_api(
        publisher: str, name: str, path: str, request: Request
    ) -> Response:
        if not webui_auth.request_authorized(request):
            return JSONResponse(
                {"detail": "Invalid or missing API token"}, status_code=401
            )
        extension_id = f"{publisher}/{name}"
        target = web.target_for(gateway, extension_id, install_dir(extension_id))
        if target is None or not target.routes:
            return JSONResponse(NOT_FOUND, status_code=404)
        declared = int(request.headers.get("content-length") or 0)
        if declared > web.MAX_BODY:
            return JSONResponse({"detail": "Body too large"}, status_code=413)
        body = await request.body()
        if len(body) > web.MAX_BODY:
            return JSONResponse({"detail": "Body too large"}, status_code=413)
        params = web.request_params(
            request.method,
            path,
            dict(request.query_params),
            dict(request.headers),
            body,
        )
        try:
            status, headers, content = await web.forward(gateway, extension_id, params)
        except LookupError:
            return JSONResponse(NOT_FOUND, status_code=404)
        except (TimeoutError, asyncio.TimeoutError):
            return JSONResponse(
                {"detail": "The extension took too long"}, status_code=504
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)
        return Response(content=content, status_code=status, headers=headers)

    @router.websocket("/x/{publisher}/{name}/ws/{path:path}")
    async def extension_ws(
        websocket: WebSocket, publisher: str, name: str, path: str
    ) -> None:
        extension_id = f"{publisher}/{name}"
        target = web.target_for(gateway, extension_id, install_dir(extension_id))
        if (
            hub is None
            or target is None
            or not target.websockets
            or not webui_auth.websocket_authorized(websocket)
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        conn_id = hub.add(
            web.WsConnection(
                extension_id,
                websocket.send_text,
                websocket.send_bytes,
                lambda code: websocket.close(code=code),
            )
        )
        if conn_id is None:
            await websocket.close(code=1013)
            return
        session = gateway.sessions.get(extension_id)
        if session is None:
            hub.remove(conn_id)
            await websocket.close(code=1011)
            return
        try:
            answer = await session.call(
                "web.ws.open",
                {
                    "conn_id": conn_id,
                    "path": path,
                    "query": dict(websocket.query_params),
                },
                timeout=web.REQUEST_TIMEOUT,
            )
            if not (isinstance(answer, dict) and answer.get("ok") is True):
                await websocket.close(code=1008)
                return
            while True:
                frame = await websocket.receive()
                if frame["type"] == "websocket.disconnect":
                    break
                if frame.get("text") is not None:
                    text = frame["text"]
                    if len(text) > web.MAX_BODY:
                        await websocket.close(code=1009)
                        break
                    params: dict[str, Any] = {"conn_id": conn_id, "text": text}
                else:
                    raw = frame.get("bytes") or b""
                    if len(raw) > web.MAX_BODY:
                        await websocket.close(code=1009)
                        break
                    params = {
                        "conn_id": conn_id,
                        "data": base64.b64encode(raw).decode("ascii"),
                    }
                if gateway.sessions.get(extension_id) is not session:
                    break
                await session.call(
                    "web.ws.message", params, timeout=web.REQUEST_TIMEOUT
                )
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:  # noqa: BLE001 - the extension failed: end this connection only
            pass
        finally:
            hub.remove(conn_id)
            live = gateway.sessions.get(extension_id)
            if live is not None:
                try:
                    await live.call("web.ws.closed", {"conn_id": conn_id}, timeout=5.0)
                except Exception:  # noqa: BLE001
                    pass

    @router.get("/x/{publisher}/{name}/{path:path}")
    async def extension_static(publisher: str, name: str, path: str) -> Response:
        return static_response(f"{publisher}/{name}", path)

    @router.get("/{alias}")
    @router.get("/{alias}/")
    @router.get("/{alias}/{path:path}")
    async def alias_static(alias: str, path: str = "") -> Response:
        owner = web.alias_owner(gateway, alias)
        if owner is None:
            return JSONResponse(NOT_FOUND, status_code=404)
        return static_response(owner, path)

    return router


def mount(
    gateway: Gateway,
    install_dir: Callable[[str], Path],
    hub: web.WebSocketHub | None = None,
) -> bool:
    """Mount the routes on the running WebUI. ``False`` if it is not up yet."""
    try:
        from core.webui import synth_webui_interface
    except Exception:  # noqa: BLE001
        return False
    if synth_webui_interface is None:
        return False
    synth_webui_interface.register_plugin_router(
        build_router(gateway, install_dir, hub), "extension web content"
    )
    synth_webui_interface.register_section_tab_provider(
        lambda section: ui.section_tabs(gateway, section)
    )
    synth_webui_interface.register_plugin_js(
        SCRIPT_NAME, SCRIPT_PATH.read_text(encoding="utf-8")
    )
    return True


async def mount_when_ready(
    gateway: Gateway,
    install_dir: Callable[[str], Path],
    hub: web.WebSocketHub | None = None,
    tries: int = 60,
) -> bool:
    for _ in range(tries):
        if mount(gateway, install_dir, hub):
            return True
        await asyncio.sleep(2)
    return False
