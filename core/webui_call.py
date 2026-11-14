"""WebUI call provider and REST/WS API.

The WebUI is the first :class:`core.call_manager.CallProvider`. A call is keyed
by the WebUI chat path (``synth_webui/<session_id>``); the avatar is already on
screen, so a "video" call needs nothing extra here — the Karada render *is* the
SyntH's webcam. The acknowledgement clip is played through the Karada state
server (``broadcast_audio``), never by iterating client connections.

Endpoints (all behind the WebUI auth middleware / token):

* ``POST /api/call/start``          ``{session_id, kind?, languages?}``
* ``POST /api/call/end``            ``{call_id}``
* ``POST /api/audio/upload``        (the normal STT endpoint) takes a ``call_id`` form
  field: when text was actually transcribed, the call's signal plays
* ``GET  /api/call/status``         ``?session_id=``
* ``WS   /api/call/screen``         ``?call_id=`` — binary JPEG screen-share frames
* ``WS   /api/call/camera``         the page's avatar as the camera of a call on another
  interface (see :mod:`core.call_video`)
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import webui_auth
from core.call_manager import (
    CallCapabilities,
    CallError,
    CallKind,
    CallProvider,
    CallSession,
    get_call_manager,
)
from core.call_ack_cache import get_call_ack_cache
from core.logging_utils import log_debug, log_info, log_warning

#: Must match ``core.webui.INTERFACE_NAME`` (kept local to avoid a circular import).
WEBUI_INTERFACE_NAME = "synth_webui"

_background: set[asyncio.Task[Any]] = set()


def webui_call_path(session_id: str) -> str:
    """Interface path of a WebUI chat session."""
    return f"{WEBUI_INTERFACE_NAME}/{session_id}"


def _wav_duration_s(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else None
    except Exception:
        return None


class WebUICallProvider(CallProvider):
    """Hosts calls in the WebUI; audio is delivered through Karada."""

    name = WEBUI_INTERFACE_NAME
    # The avatar is always visible in the WebUI, so video == the Karada render.
    capabilities = CallCapabilities(audio=True, video=True, screen_share=True)

    async def play_audio(self, session: CallSession, path: Path) -> None:
        from core.animation_handler import get_karada_state_server

        # A signal, not speech: no lip-sync, no TALK animation, no chat bubble.
        await get_karada_state_server().broadcast_sfx(
            str(path), audio_duration_s=_wav_duration_s(path)
        )

    def is_agent_speaking(self, session: CallSession) -> bool:
        try:
            from core.animation_handler import get_karada_state_server

            return get_karada_state_server().get_current_audio() is not None
        except Exception:
            return False


def resolve_call_for_session(
    call_id: str | None, session_id: str
) -> CallSession | None:
    """Return the active call ``call_id`` only if it belongs to ``session_id``."""
    session = get_call_manager().get_session(call_id)
    if session is None or session.interface_path != webui_call_path(session_id):
        return None
    return session


async def end_calls_for_session(session_id: str) -> None:
    """End any call on this WebUI session (its last socket went away)."""
    mgr = get_call_manager()
    session = mgr.get_active_call(webui_call_path(session_id))
    if session is not None:
        await mgr.end_call(session.call_id)


def notify_transcribed(call_id: str | None) -> None:
    """Text was transcribed from the caller's speech: play the signal (fire-and-forget)."""
    if not call_id:
        return
    task = asyncio.ensure_future(get_call_manager().on_message_transcribed(call_id))
    _background.add(task)
    task.add_done_callback(_background.discard)


class StartCallBody(BaseModel):
    session_id: str
    kind: str = "audio"
    languages: list[str] = Field(default_factory=list)


class EndCallBody(BaseModel):
    call_id: str


def readiness_warnings() -> list[str]:
    """Reasons a call cannot work properly right now (empty when all is well).

    A call needs speech recognition (Auris) to hear the caller and voice (Vox) to
    answer. Best-effort: an engine that is selected but
    broken (e.g. its package is missing) is not detectable here; the STT error
    names the engine instead.
    """
    warnings: list[str] = []
    try:
        from core.core_initializer import PLUGIN_REGISTRY

        auris = PLUGIN_REGISTRY.get("auris_plugin")
        if auris is None or getattr(auris, "_active_engine_name", "") in (
            "disabled",
            "inline",
        ):
            warnings.append(
                "Speech recognition (Auris) is off: the SyntH cannot hear you in a call."
            )
        vox = PLUGIN_REGISTRY.get("vox_plugin") or PLUGIN_REGISTRY.get("vox")
        if vox is None or getattr(vox, "_active_engine_name", "") == "disabled":
            warnings.append(
                "Voice (Vox) is disabled: the SyntH cannot speak in a call."
            )
    except Exception as exc:  # pragma: no cover - defensive
        log_debug(f"[call] readiness check skipped: {exc}")
    return warnings


def _describe(session: CallSession) -> dict[str, Any]:
    return {
        "call_id": session.call_id,
        "kind": session.kind.value,
        "degraded": session.degraded,
        "state": session.state.value,
        # Is the "Hmm" clip ready yet? (it is made in the background at call start)
        "ack_ready": get_call_ack_cache().get(
            session.languages[0] if session.languages else None
        )
        is not None,
        "last_ack": session.last_ack,
    }


def create_call_router() -> APIRouter:
    router = APIRouter()
    mgr = get_call_manager

    @router.post("/api/call/start")
    async def start_call(body: StartCallBody) -> JSONResponse:
        try:
            kind = CallKind(body.kind.lower())
        except ValueError:
            return JSONResponse({"error": f"unknown call kind '{body.kind}'"}, 400)
        try:
            session = await mgr().request_call(
                WEBUI_INTERFACE_NAME,
                webui_call_path(body.session_id),
                kind=kind,
                user_id=body.session_id,
                languages=body.languages,
            )
        except CallError as exc:
            return JSONResponse({"error": str(exc)}, 503)
        info = _describe(session)
        info["warnings"] = readiness_warnings()
        for warning in info["warnings"]:
            log_warning(f"[call] {session.call_id}: {warning}")
        return JSONResponse(info)

    @router.post("/api/call/end")
    async def end_call(body: EndCallBody) -> JSONResponse:
        ended = await mgr().end_call(body.call_id)
        return JSONResponse({"ended": ended}, 200 if ended else 404)

    @router.get("/api/call/status")
    async def call_status(session_id: str) -> JSONResponse:
        session = mgr().get_active_call(webui_call_path(session_id))
        return JSONResponse({"call": _describe(session) if session else None})

    @router.websocket("/api/call/screen")
    async def screen_ws(websocket: WebSocket, call_id: str) -> None:
        if not webui_auth.websocket_authorized(websocket):
            await websocket.close(code=4401, reason="Invalid or missing API token")
            return
        if mgr().get_session(call_id) is None:
            await websocket.close(code=4404, reason="Unknown call")
            return
        await websocket.accept()
        try:
            while True:
                frame = await websocket.receive_bytes()
                mgr().submit_screen_frame(call_id, frame)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log_debug(f"[call] screen stream closed: {exc}")

    return router


def mount_call_api(app: FastAPI) -> None:
    """Register the WebUI call provider and mount the call API on ``app``."""
    try:
        get_call_manager().register_provider(WebUICallProvider())
        app.include_router(create_call_router())
        # The camera socket lets other interfaces' calls borrow this page's avatar.
        from core.call_video import mount_call_video_api

        mount_call_video_api(app)
        log_info("[call] WebUI call API mounted at /api/call/")
    except Exception as exc:
        log_warning(f"[call] WebUI call API failed to mount (non-fatal): {exc}")
