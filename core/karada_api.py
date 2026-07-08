"""Karada State Server — public REST + WebSocket API.

Exposes ``/api/karada/*`` endpoints that any external client (OBS, Unity,
XR headset, mobile app, Home Assistant) can use to observe and interact
with the shared avatar body.

Usage::

    from core.karada_api import create_karada_router

    rest_router, ws_router = create_karada_router(animation_handler)
    app.include_router(rest_router)
    app.include_router(ws_router)
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse

from core.animation_handler import AnimationState
from core.karada_transport import KaradaTransport
from core.logging_utils import log_info, log_warning

if TYPE_CHECKING:
    from core.animation_handler import KaradaStateServer


def _configured_api_token() -> Optional[str]:
    """Optional bearer token gating ``/api/karada/*``.

    Unset (default) means no auth — matches the rest of the WebUI surface,
    which has no auth layer today. Set ``SYNTH_WEBUI_API_TOKEN`` to require
    ``Authorization: Bearer <token>`` or ``?token=<token>`` on every request.
    """
    token = os.getenv("SYNTH_WEBUI_API_TOKEN", "").strip()
    return token or None


def _token_from_request(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token")


async def _require_api_token(request: Request) -> None:
    expected = _configured_api_token()
    if expected is None:
        return
    if _token_from_request(request) != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


def _token_from_websocket(websocket: WebSocket) -> Optional[str]:
    auth = websocket.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return websocket.query_params.get("token")


# ---------------------------------------------------------------------------
# WebSocket transport for Karada API clients
# ---------------------------------------------------------------------------


class KaradaApiTransport(KaradaTransport):
    """Transport that broadcasts to WebSocket clients connected via ``/api/karada/ws``."""

    def __init__(self) -> None:
        self._connections: Dict[str, WebSocket] = {}

    def add(self, session_id: str, ws: WebSocket) -> None:
        self._connections[session_id] = ws

    def remove(self, session_id: str) -> None:
        self._connections.pop(session_id, None)

    # -- broadcast helpers ---------------------------------------------------

    async def _broadcast(self, payload: Dict[str, Any], label: str) -> None:
        for sid, ws in list(self._connections.items()):
            try:
                await ws.send_json(payload)
            except Exception as exc:
                log_warning(f"[KaradaAPI] Failed to {label} to {sid}: {exc}")

    async def broadcast_animation(self, payload: Dict[str, Any]) -> None:
        await self._broadcast(payload, "broadcast animation")

    async def broadcast_audio(self, payload: Dict[str, Any]) -> None:
        await self._broadcast(payload, "broadcast audio")

    async def broadcast_face(self, payload: Dict[str, Any]) -> None:
        await self._broadcast(payload, "broadcast face")

    async def broadcast_model(self, payload: Dict[str, Any]) -> None:
        await self._broadcast(payload, "broadcast model")

    async def broadcast_expression(self, payload: Dict[str, Any]) -> None:
        await self._broadcast(payload, "broadcast expression")

    async def send_to_session(self, session_id: str, payload: Dict[str, Any]) -> None:
        ws = self._connections.get(session_id)
        if ws:
            try:
                await ws.send_json(payload)
            except Exception as exc:
                log_warning(f"[KaradaAPI] send_to_session {session_id} failed: {exc}")

    async def preload_asset(
        self, session_id: Optional[str], payload: Dict[str, Any]
    ) -> None:
        if session_id is None:
            await self._broadcast(payload, "preload")
        else:
            await self.send_to_session(session_id, payload)

    def get_connected_sessions(self) -> List[str]:
        return list(self._connections.keys())


# ---------------------------------------------------------------------------
# Asset manifest builder
# ---------------------------------------------------------------------------

_manifest_cache: Optional[Dict[str, Any]] = None


def _build_asset_manifest(skins_root: Path) -> Dict[str, Any]:
    """Walk *skins_root* and hash every animation / model / descriptor file."""
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache

    assets: Dict[str, Dict[str, Any]] = {}
    extensions = {".fbx", ".vrm", ".json", ".png"}
    for p in skins_root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in extensions:
            continue
        try:
            rel = f"/skins/{p.relative_to(skins_root).as_posix()}"
            size = p.stat().st_size
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            assets[rel] = {"hash": f"sha256:{h}", "size": size}
        except Exception:
            continue

    _manifest_cache = {"version": 1, "assets": assets}
    return _manifest_cache


def invalidate_manifest_cache() -> None:
    """Force the next ``/api/karada/assets/manifest`` call to rebuild."""
    global _manifest_cache
    _manifest_cache = None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_karada_router(
    handler: "KaradaStateServer",
    skins_root: Optional[Path] = None,
) -> tuple[APIRouter, APIRouter]:
    """Create the FastAPI routers with all Karada public endpoints.

    Returns a ``(rest_router, ws_router)`` pair — callers must
    ``app.include_router()`` both. They are kept separate because
    FastAPI/Starlette applies an ``APIRouter``'s own ``dependencies=`` to
    every route added to it, including websocket routes; a ``Depends()``
    typed for HTTP ``Request`` cannot resolve against a websocket-scope ASGI
    connection and raises inside dependency resolution instead of cleanly
    rejecting the handshake. The WS route does its own token check instead
    (see ``karada_ws`` below).

    Args:
        handler: The :class:`KaradaStateServer` singleton.
        skins_root: Path to the ``skins/`` directory.  If ``None`` it is
                    inferred as ``<project_root>/skins``.
    """
    router = APIRouter(
        prefix="/api/karada",
        tags=["karada"],
        dependencies=[Depends(_require_api_token)],
    )
    ws_router = APIRouter(prefix="/api/karada", tags=["karada"])
    if skins_root is None:
        skins_root = Path(__file__).resolve().parent.parent / "skins"

    api_transport = KaradaApiTransport()
    handler.add_transport(api_transport)

    # == Full state =========================================================

    @router.get("/state")
    async def get_full_state() -> JSONResponse:
        """Return the complete Karada state (animation + model + face + audio)."""
        state = await handler.get_full_state()
        return JSONResponse(content=state)

    @router.get("/state/animation")
    async def get_animation_state() -> JSONResponse:
        """Return just the current animation state."""
        info = handler.get_current_animation_state()
        return JSONResponse(content=info)

    @router.get("/animations/manifest")
    async def get_animation_manifest() -> JSONResponse:
        """Return the Karada animation manifest keyed by descriptor id."""
        return JSONResponse(content=handler.get_animation_manifest())

    @router.get("/animations/resolve")
    async def resolve_animation_contract(descriptor_id: str) -> JSONResponse:
        """Resolve one descriptor id into an animation manifest entry."""
        entry = handler.get_animation_manifest_entry_by_id(descriptor_id)
        if entry is None:
            return JSONResponse(
                status_code=404, content={"error": "descriptor_not_found"}
            )
        return JSONResponse(content=entry)

    @router.get("/state/model")
    async def get_model_state() -> JSONResponse:
        """Return the active VRM model info."""
        full = await handler.get_full_state()
        return JSONResponse(content=full.get("vrm_model", {}))

    @router.get("/state/face")
    async def get_face_state() -> JSONResponse:
        """Return the current face blend-shape values."""
        full = await handler.get_full_state()
        return JSONResponse(content=full.get("face_values", {}))

    @router.get("/state/audio")
    async def get_audio_state() -> JSONResponse:
        """Return the current TTS audio state (or null)."""
        audio = handler.get_current_audio()
        return JSONResponse(content=audio or {})

    # == Action request =====================================================

    @router.post("/action")
    async def post_action(body: Dict[str, Any]) -> JSONResponse:
        """Request an animation action (e.g. touch, emote).

        Body: ``{"action": "think", "priority": 10, "context_id": "..."}``
        """
        action_name: str = body.get("action", "idle")
        priority: Optional[int] = body.get("priority")
        context_id: Optional[str] = body.get("context_id")
        loop: bool = body.get("loop", True)

        try:
            state_enum = AnimationState(action_name)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": f"Unknown action: {action_name}"},
            )

        await handler.play_animation(
            state_enum,
            session_id=None,
            loop=loop,
            context_id=context_id,
            priority=priority,
            source="karada_api",
        )
        return JSONResponse(content={"status": "ok", "action": action_name})

    # == Animation discovery ================================================

    @router.get("/animations/{state_name}")
    async def get_animations_for_state(state_name: str) -> JSONResponse:
        """List animation variants available for a given state."""
        variants = handler.get_animation_variants(state_name)
        return JSONResponse(content=variants)

    @router.get("/animations/{state_name}/{filename}/descriptor")
    async def get_animation_descriptor(state_name: str, filename: str) -> JSONResponse:
        """Return the descriptor for a specific animation file."""
        desc_path = skins_root / "Rei" / "animations" / state_name / f"{filename}.json"
        if desc_path.exists():
            try:
                return JSONResponse(content=json.loads(desc_path.read_text()))
            except Exception:
                pass
        # Try resolving via the handler
        try:
            _, descriptor = handler._resolve_animation_descriptor_for_state(
                filename, state_name
            )
            if descriptor:
                return JSONResponse(content=descriptor)
        except Exception:
            pass
        return JSONResponse(content={})

    # == Skins ==============================================================

    @router.get("/skins")
    async def list_skins() -> JSONResponse:
        """List available skins with their metadata."""
        result: List[Dict[str, Any]] = []
        if skins_root.exists():
            for d in sorted(skins_root.iterdir()):
                if not d.is_dir() or d.name.startswith(".") or d.name == "temp":
                    continue
                info: Dict[str, Any] = {"name": d.name}
                persona_json = d / "persona.json"
                if persona_json.exists():
                    try:
                        info["persona"] = json.loads(persona_json.read_text())
                    except Exception:
                        pass
                model = d / "model.vrm"
                info["has_model"] = model.exists()
                info["has_animations"] = (d / "animations").is_dir()
                result.append(info)
        return JSONResponse(content=result)

    # == Assets =============================================================

    @router.get("/assets/manifest")
    async def get_asset_manifest() -> JSONResponse:
        """Return a SHA-256 manifest of all skin assets."""
        manifest = _build_asset_manifest(skins_root)
        return JSONResponse(content=manifest)

    @router.post("/assets/missing")
    async def get_missing_assets(body: Dict[str, Any]) -> JSONResponse:
        """Given a list of asset hashes the client already has, return missing ones.

        Body: ``{"has_assets": ["/skins/Rei/model.vrm", ...]}``
        """
        has_set = set(body.get("has_assets", []))
        manifest = _build_asset_manifest(skins_root)
        missing = [
            {"path": path, **meta}
            for path, meta in manifest.get("assets", {}).items()
            if path not in has_set
        ]
        return JSONResponse(content={"missing": missing})

    # == WebSocket stream ===================================================

    @ws_router.websocket("/ws")
    async def karada_ws(websocket: WebSocket) -> None:
        """Dedicated WebSocket for Karada state streaming.

        Protocol::

            Client → {"type": "hello", "capabilities": [...], "has_assets": [...]}
            Server → {"type": "state_full", ...}
            Server → {"type": "animation", ...}   (ongoing stream)
            Server → {"type": "audio", ...}
            Server → {"type": "face", ...}
            Server → {"type": "model", ...}
            Client → {"type": "action", "action": "touch", ...}
        """
        expected_token = _configured_api_token()
        if (
            expected_token is not None
            and _token_from_websocket(websocket) != expected_token
        ):
            await websocket.close(code=4401, reason="Invalid or missing API token")
            return

        await websocket.accept()
        import uuid as _uuid

        session_id = f"karada_{_uuid.uuid4().hex[:8]}"
        api_transport.add(session_id, websocket)
        log_info(f"[KaradaAPI] Client connected: {session_id}")

        try:
            # Push full state immediately
            full = await handler.get_full_state()
            await websocket.send_json({"type": "state_full", **full})

            # Message loop
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "action":
                    action_name = msg.get("action", "idle")
                    try:
                        state_enum = AnimationState(action_name)
                    except ValueError:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": f"Unknown action: {action_name}",
                            }
                        )
                        continue
                    await handler.play_animation(
                        state_enum,
                        session_id=None,
                        loop=msg.get("loop", True),
                        context_id=msg.get("context_id"),
                        priority=msg.get("priority"),
                        source="karada_ws",
                    )

                elif msg_type == "hello":
                    # Re-send full state after hello
                    full = await handler.get_full_state()
                    await websocket.send_json({"type": "state_full", **full})

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log_warning(f"[KaradaAPI] WebSocket error for {session_id}: {exc}")
        finally:
            api_transport.remove(session_id)
            log_info(f"[KaradaAPI] Client disconnected: {session_id}")

    return router, ws_router
