"""FastAPI-based web interface branded as the Synthetic Heart Web UI.

This is a core component of Synthetic Heart that provides a functional chat front-end
integrating with the existing synth core infrastructure. It offers a refined
layout, VRM avatar management, and notification helpers so the Docker container
can serve the application directly.

Note: This was moved from interface/ to core/ as it's now considered an
integral and inseparable part of the SyntH system.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import uuid
import platform
import tempfile
import base64
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, Optional, List, Any
from urllib.parse import quote, unquote, urlparse

from fastapi import (
    Depends,
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    UploadFile,
    File,
    Form,
    Request,
    HTTPException,
)
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from core.core_initializer import register_interface
from core.logging_utils import _LOG_FILE, log_debug, log_error, log_info, log_warning
from core.config_manager import config_registry
from core.variables_engine import register_exposed_var
from core.message_chain import (
    get_failed_message_text,
    RESPONSE_TIMEOUT,
)
from core import db as core_db
from core.action_state_manager import get_action_state_manager, AnimationPhase
from core.animation_handler import (
    AnimationState,
    KaradaStateServer,
    set_karada_state_server,
)
from core.karada_ws_transport import WebSocketTransport
from core import animation_uploads
import mimetypes


BRAND_NAME = "Synthetic Heart"
INTERFACE_NAME = "synth_webui"
EXTERNAL_ENDPOINT_PROBE_TIMEOUT_SECONDS = 300.0

# exposed configuration variable to toggle experimental multi-session mode
register_exposed_var(
    "MULTI_SESSION",
    label="Enable multi-session WebUI (experimental)",
    default=False,
    value_type=bool,
    ui_type="boolean",
    description=(
        "When true each browser connection receives its own session ID. "
        "This is an experimental mode; history/animation state is not "
        "persisted across container restarts and behaviour may be unstable."
    ),
    scope="core",
    component="webui",
    advanced=True,
)
LOG_PREFIX = "[synth_webui]"
WEBUI_LOG = "webui"  # Log file name for WebUI (logs/webui.log)
# Internal chat/component identifier used when interacting with the LLM and
# action state manager. This must remain "webui" for compatibility with
# engines and tests which expect the internal component name to be "webui".
INTERNAL_CHAT_NAME = "webui"
_LEGACY_AUTOSTART_ENV = "WEBWAIFU_AUTOSTART"
_AUTOSTART_ENV = "SYNTH_WEBUI_AUTOSTART"
_LEGACY_VRM_DIR_ENV = "WEBWAIFU_VRM_DIR"
_VRM_DIR_ENV = "SYNTH_WEBUI_VRM_DIR"
_WEBUI_TOUCH_CONTEXT_ID = "__webui_touch_overlay"
_WEBUI_TOUCH_PRIORITY = 11


# Ensure correct MIME types are registered
mimetypes.init()
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/json", ".json")


def _clean_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read an env var, stripping inline ``# comment`` suffixes and whitespace.

    Some launchers (notably the VS Code/Antigravity Python integration, which
    injects the workspace ``.env`` with its own parser) leave inline comments
    inside the value, e.g. ``'8088   # HTTPS port'``. Because the app loads
    ``.env`` with ``override=False``, such a poisoned value wins over the
    correctly parsed file — so sanitize here and warn instead of failing
    silently downstream (see AGENTS.md §12, 2026-07-09).
    """
    raw = os.getenv(name, default)
    if raw is None:
        return None
    cleaned = raw.split("#", 1)[0].strip()
    if cleaned != raw.strip():
        log_warning(
            f"{LOG_PREFIX} Env var {name} contained an inline comment; "
            f"using {cleaned!r} (raw value was {raw!r})"
        )
    return cleaned


class SynthWebUIInterface:
    """Production-ready web interface served from the Docker container."""

    display_name = "Web UI"

    def __init__(self, autostart: bool = True) -> None:
        self.app = FastAPI(title=BRAND_NAME, version="1.0")
        self.start_time = datetime.now(tz=timezone.utc)

        # Lightweight request logger middleware to capture client static/resource requests
        try:
            from starlette.middleware.base import BaseHTTPMiddleware

            class _RequestLoggerMiddleware(BaseHTTPMiddleware):
                async def dispatch(inner_self, request, call_next):
                    response = await call_next(request)
                    try:
                        path = request.url.path or ""
                        interesting = (
                            path.startswith("/js")
                            or path.startswith("/static")
                            or path == "/service-worker.js"
                            or path.startswith("/api")
                        )
                        if interesting:
                            log_info(
                                f"{LOG_PREFIX} HTTP {request.method} {path} -> {response.status_code}",
                                log_file=WEBUI_LOG,
                            )
                    except Exception:
                        pass
                    return response

            self.app.add_middleware(_RequestLoggerMiddleware)  # type: ignore[arg-type]
        except Exception:
            # Don't fail initialization if middleware cannot be added
            pass
        self.connections: Dict[str, WebSocket] = {}
        self.message_history: Dict[str, Deque[dict]] = {}
        self.max_history = 100
        # Mate Engine outbox (messages sent from SyntH to Mate)
        self._mate_outbox: Deque[dict] = deque(maxlen=500)
        import asyncio as _asyncio

        self._mate_outbox_lock = _asyncio.Lock()
        # Generic integration outboxes keyed by source (e.g., 'mate', 'other')
        self._integration_outboxes: Dict[str, Deque[dict]] = {}
        self._integration_outbox_locks: Dict[str, Any] = {}
        # Helper to create per-source outbox (see ensure_integration_outbox method)
        # Track pending THINKING actions per session so we can deterministically
        # switch THINK -> WRITE -> IDLE when the async response is actually sent.
        self._pending_thinking_actions: Dict[str, Deque[str]] = {}
        # Active skin tracker (folder name) – used by the new /api/skins/current_skin
        # endpoint.  This is a hint for situations where the active VRM has lost
        # its original path (e.g. after copying into avatars); for other cases the
        # value is derived dynamically from `self.active_vrm`.
        self._current_skin: Optional[str] = None
        # Track active WRITING actions per session so we can stop them immediately
        # after sending, and avoid starting WRITING too late.
        self._active_writing_actions: Dict[str, Deque[str]] = {}
        # Track when THINKING started per session so we can ensure it's visible
        # before switching to WRITING at generation_start.
        self._thinking_started_at_ms: Dict[str, int] = {}
        # Runtime/configurable attributes with sensible defaults
        # Autostart can be disabled for tests/dev harnesses.
        self.autostart = bool(autostart)
        self.host = _clean_env("SYNTH_WEBUI_HOST", "0.0.0.0") or "0.0.0.0"
        self.log_level = os.getenv("SYNTH_WEBUI_LOG_LEVEL", "info")
        # TLS / HTTPS configuration
        # By default expose the WebUI over HTTPS unless explicitly disabled.
        # This makes the default developer experience minimal and secure.
        tls_flag = _clean_env("SYNTH_WEBUI_TLS")
        if tls_flag is None:
            tls_flag = _clean_env("SECURE_CONNECTION", "1")
        self.tls_enabled = tls_flag == "1"
        self.tls_certfile = os.getenv("SYNTH_WEBUI_CERTFILE", None)
        self.tls_keyfile = os.getenv("SYNTH_WEBUI_KEYFILE", None)
        # Port configuration
        # - SYNTH_WEBUI_HTTP_PORT: plain HTTP port
        # - SYNTH_WEBUI_HTTPS_PORT: HTTPS/TLS port (only used when TLS is enabled)
        # Backward compatible fallbacks:
        # - SYNTH_WEBUI_PORT / PORT
        raw_http_port = _clean_env("SYNTH_WEBUI_HTTP_PORT")
        if raw_http_port is None:
            raw_http_port = _clean_env("SYNTH_WEBUI_PORT")
        if raw_http_port is None:
            raw_http_port = _clean_env("PORT", "8080")
        try:
            http_port = int(raw_http_port or "8080")
        except Exception:
            log_warning(
                f"{LOG_PREFIX} Could not parse HTTP port {raw_http_port!r}; "
                f"falling back to 8080"
            )
            http_port = 8080

        https_port = None
        if self.tls_enabled:
            raw_https_port = _clean_env("SYNTH_WEBUI_HTTPS_PORT")
            if raw_https_port:
                try:
                    https_port = int(raw_https_port)
                except Exception:
                    log_warning(
                        f"{LOG_PREFIX} Could not parse HTTPS port "
                        f"{raw_https_port!r}; serving HTTPS on the HTTP port "
                        f"{http_port} instead"
                    )
                    https_port = http_port
            else:
                # If no explicit HTTPS port is provided, keep historical behavior
                # (serve HTTPS on the HTTP port).
                https_port = http_port

        # Main server port
        self.port = https_port if self.tls_enabled else http_port

        # Optional HTTP port to serve plain HTTP alongside HTTPS (useful for dev/testing)
        self.http_port = (
            http_port if (self.tls_enabled and http_port != self.port) else None
        )
        # Log streaming options
        self.log_source_path = None
        self.log_wait_seconds = 20
        # Server control placeholders
        self._server_lock = threading.Lock()
        self._server = None
        self._server_thread = None
        self._server_task = None
        # Persistent session id file (single session per deploy)
        # Allow override of backups directory in tests or custom deploys via env var
        backups_dir = os.environ.get("SYNTH_BACKUPS_DIR", "backups")
        self.session_id_file = Path(backups_dir) / "webui_session_id.txt"
        self.session_id = None
        # helper value cache for multi-session flag (not used - evaluations are cheap)
        # self._multi_session_cache: Optional[bool] = None
        try:
            self._ensure_persistent_session_id()
        except Exception:
            log_warning(f"{LOG_PREFIX} Unable to initialize persistent session id")

        # Temporary animation upload cleanup settings
        try:
            self.mate_upload_ttl_days = int(
                os.getenv("SYNTH_MATEENGINE_UPLOAD_TTL_DAYS", "7")
            )
        except Exception:
            self.mate_upload_ttl_days = 7
        try:
            self.mate_upload_cleanup_interval_s = int(
                os.getenv("SYNTH_MATEENGINE_UPLOAD_CLEANUP_INTERVAL_S", "3600")
            )
        except Exception:
            self.mate_upload_cleanup_interval_s = 3600
        self._uploads_cleanup_task: Optional[asyncio.Task] = None

        # Static and VRM directories used by the Web UI. These are calculated
        # relative to the repository layout and can be overridden using the
        # SYNTH_WEBUI_VRM_DIR environment variable for deployments.
        base_res = Path(__file__).resolve().parent.parent / "res" / "synth_webui"
        static_dir = base_res / "static"

        # VRM directory: default to skins/temp (deterministic upload location)
        env_vrm = os.getenv(_VRM_DIR_ENV) or os.getenv(_LEGACY_VRM_DIR_ENV)
        if env_vrm:
            self.vrm_dir = Path(env_vrm).expanduser()
        else:
            # store uploaded VRMs in skins/temp to be deterministic
            self.vrm_dir = Path("skins/temp")
        # Ensure parent exists
        try:
            self.vrm_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Non-fatal; operations will check existence before use
            pass

        # Active VRM marker file path
        self.active_vrm_marker = self.vrm_dir / ".active"
        # Load active VRM from marker or default
        self.active_vrm = self._load_active_vrm()
        # initialise skin hint based on whatever active_vrm we found
        self._current_skin = self._derive_skin_from_active_vrm()

        # Attachments storage: prefer explicit env var, then XDG_DATA_HOME,
        # then a platform default. On Windows, /config becomes a drive-root
        # path, so prefer a temp-backed local directory instead.
        attachments_root = os.getenv("SYNTH_ATTACHMENTS_ROOT")
        if attachments_root:
            self.attachments_dir = Path(attachments_root).expanduser()
            log_info(
                f"{LOG_PREFIX} Using attachments directory from SYNTH_ATTACHMENTS_ROOT: {self.attachments_dir}",
                log_file=WEBUI_LOG,
            )
        else:
            xdg_data_home = os.getenv("XDG_DATA_HOME")
            if xdg_data_home:
                self.attachments_dir = Path(xdg_data_home).expanduser() / "attachments"
                log_info(
                    f"{LOG_PREFIX} Using attachments directory from XDG_DATA_HOME: {self.attachments_dir}",
                    log_file=WEBUI_LOG,
                )
            elif os.name == "nt":
                self.attachments_dir = (
                    Path(tempfile.gettempdir()) / "synth_webui" / "attachments"
                )
                log_info(
                    f"{LOG_PREFIX} Using Windows attachments directory: {self.attachments_dir}",
                    log_file=WEBUI_LOG,
                )
            else:
                self.attachments_dir = Path("/config") / "uploads"
                log_info(
                    f"{LOG_PREFIX} Using default attachments directory: {self.attachments_dir}",
                    log_file=WEBUI_LOG,
                )
        try:
            self.attachments_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log_warning(
                f"{LOG_PREFIX} Could not create attachments directory {self.attachments_dir}: {exc}",
                log_file=WEBUI_LOG,
            )
            fallback_dir = Path(tempfile.gettempdir()) / "synth_webui" / "attachments"
            try:
                fallback_dir.mkdir(parents=True, exist_ok=True)
                self.attachments_dir = fallback_dir
                log_info(
                    f"{LOG_PREFIX} Falling back to attachments directory {self.attachments_dir}",
                    log_file=WEBUI_LOG,
                )
                log_warning(
                    f"{LOG_PREFIX} Uploaded files will not persist in temporary attachments directory {self.attachments_dir}",
                    log_file=WEBUI_LOG,
                )
            except Exception as exc2:
                log_warning(
                    f"{LOG_PREFIX} Could not create fallback attachments directory {fallback_dir}: {exc2}",
                    log_file=WEBUI_LOG,
                )

        if static_dir.exists():
            self.app.mount(
                "/static", StaticFiles(directory=str(static_dir)), name="static"
            )
        else:
            log_warning(
                f"{LOG_PREFIX} static directory not found: {static_dir}",
                log_file=WEBUI_LOG,
            )

        # Ensure the root path always returns the rendered HTML directly.
        # In some deployment or hot-reload scenarios a previous handler may
        # end up returning None (serialized as JSON null). Add a lightweight
        # middleware that intercepts '/' and returns the rendered index to
        # guarantee consistent behaviour.
        try:
            from starlette.middleware.base import BaseHTTPMiddleware

            class _IndexMiddleware(BaseHTTPMiddleware):
                async def dispatch(inner_self, request, call_next):
                    if request.url.path == "/":
                        log_info(
                            f"{LOG_PREFIX} Index middleware intercepting root request"
                        )
                        try:
                            content = self._render_index()
                            log_info(
                                f"{LOG_PREFIX} Index middleware rendered length {len(content)}"
                            )
                            return HTMLResponse(content=content, media_type="text/html")
                        except Exception as e:
                            log_error(
                                f"{LOG_PREFIX} Index middleware failed to render index: {e}"
                            )
                            raise
                    return await call_next(request)

            self.app.add_middleware(_IndexMiddleware)
        except Exception as e:
            log_warning(f"{LOG_PREFIX} Failed to add Index middleware: {e}")

        # Mount JS directory for Mixamo animations (separate mount to avoid path conflicts)
        js_dir = Path(__file__).resolve().parent.parent / "res" / "synth_webui" / "js"
        if js_dir.exists():
            self.app.mount(
                "/js", StaticFiles(directory=str(js_dir)), name="synth-webui-js"
            )
            log_info(f"{LOG_PREFIX} Mounted /js to {js_dir}", log_file=WEBUI_LOG)
        else:
            log_warning(
                f"{LOG_PREFIX} JS directory not found: {js_dir}", log_file=WEBUI_LOG
            )

        # middleware to ensure static/js files are not aggressively cached; this
        # makes curl or browsers always fetch the latest version even if query
        # string doesn't change (useful during development or when clients are
        # misbehaving with their cache).
        try:
            from starlette.middleware.base import BaseHTTPMiddleware

            class _NoCacheMiddleware(BaseHTTPMiddleware):
                async def dispatch(inner_self, request, call_next):
                    response = await call_next(request)
                    path = request.url.path
                    if (
                        path.startswith("/js/")
                        or path.startswith("/static/")
                        or path.startswith("/skins")
                        or path.startswith("/uploads")
                    ):
                        # No store ensures proxies and browsers always revalidate.
                        response.headers["Cache-Control"] = "no-cache"
                    return response

            self.app.add_middleware(_NoCacheMiddleware)
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Failed to add no-cache middleware: {exc}")

        # Use the bundled static logo path. The image is expected to be present
        # in the image under /app/res/synth_webui/static/synth_logo_bg.png.
        self.logo_url = "/static/synth_logo_bg.png"

        # No global animations directory: animations live inside each skin under /skins/<skin>/animations

        # Mount skins directory (contains per-skin assets: preview, animations, md)
        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        if skins_dir.exists():
            try:
                self.app.mount(
                    "/skins",
                    StaticFiles(directory=str(skins_dir)),
                    name="synth-webui-skins",
                )
                log_info(f"{LOG_PREFIX} Mounted /skins to {skins_dir}")
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Failed to mount /skins: {exc}")
        else:
            log_warning(f"{LOG_PREFIX} Skins directory not found: {skins_dir}")
        if self.attachments_dir.exists():
            try:
                self.app.mount(
                    "/uploads",
                    StaticFiles(directory=str(self.attachments_dir)),
                    name="synth-webui-uploads",
                )
                log_info(f"{LOG_PREFIX} Mounted /uploads to {self.attachments_dir}")
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Failed to mount /uploads: {exc}")
        else:
            log_warning(
                f"{LOG_PREFIX} Attachments directory does not exist, /uploads endpoint NOT mounted"
            )
        if self.vrm_dir.exists():
            try:
                self.app.mount(
                    "/avatars",
                    StaticFiles(directory=str(self.vrm_dir)),
                    name="synth-webui-avatars",
                )
                log_info(f"{LOG_PREFIX} Mounted /avatars to {self.vrm_dir}")
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Failed to mount /avatars: {exc}")
        else:
            log_warning(
                f"{LOG_PREFIX} VRM directory does not exist, /avatars endpoint NOT mounted"
            )

        # Mount the SyntH Stage frontend (frontend/dist) at /stage when built.
        # The Stage app is an optional standalone Vue client (see frontend/README.md);
        # the backend runs fine without it, so this mount is best-effort.
        stage_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
        if stage_dist.exists():
            try:
                self.app.mount(
                    "/stage",
                    StaticFiles(directory=str(stage_dist), html=True),
                    name="synth-stage",
                )
                log_info(f"{LOG_PREFIX} Mounted /stage to {stage_dist}")
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Failed to mount /stage: {exc}")
        else:
            log_info(
                f"{LOG_PREFIX} Stage frontend not built, /stage NOT mounted ({stage_dist})"
            )

        # Optional CORS for cross-origin Stage clients (e.g. Capacitor apps).
        # Gated on SYNTH_WEBUI_CORS_ORIGINS (comma-separated origins); default
        # empty -> middleware not added, existing behaviour unchanged. Same-origin
        # deployments (/stage) and the Vite dev proxy do not need this.
        cors_origins = [
            origin.strip()
            for origin in os.getenv("SYNTH_WEBUI_CORS_ORIGINS", "").split(",")
            if origin.strip()
        ]
        if cors_origins:
            try:
                from starlette.middleware.cors import CORSMiddleware

                self.app.add_middleware(
                    CORSMiddleware,  # type: ignore[arg-type]
                    allow_origins=cors_origins,
                    allow_credentials=True,
                    allow_methods=["*"],
                    allow_headers=["*"],
                )
                log_info(f"{LOG_PREFIX} CORS enabled for origins: {cors_origins}")
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Failed to add CORS middleware: {exc}")

        if self.vrm_dir.exists():
            log_debug(f"{LOG_PREFIX} VRM directory is_dir: {self.vrm_dir.is_dir()}")
            log_debug(
                f"{LOG_PREFIX} VRM directory is readable: {os.access(self.vrm_dir, os.R_OK)}"
            )

            try:
                files = list(self.vrm_dir.iterdir())
                log_info(f"{LOG_PREFIX} VRM directory contains {len(files)} items:")
                for item in files:
                    file_type = "file" if item.is_file() else "dir"
                    size = item.stat().st_size if item.is_file() else "N/A"
                    log_info(
                        f"{LOG_PREFIX}   - {item.name} ({file_type}, {size} bytes)"
                    )
            except Exception as list_exc:
                log_warning(
                    f"{LOG_PREFIX} Unable to list VRM directory contents: {list_exc}"
                )
        log_info(f"{LOG_PREFIX} ========== VRM DIRECTORY MOUNT END ==========")

        # Initialize KaradaStateServer so tests and runtime can access it
        try:
            self.animation_handler = KaradaStateServer(self)
            # Register webui callbacks with the KaradaStateServer
            self.animation_handler.set_webui(self)
            # Register WebSocket transport so KaradaStateServer can broadcast
            ws_transport = WebSocketTransport(self.connections)
            self.animation_handler.add_transport(ws_transport)
            # Publish as global singleton so plugins (e.g. FacialExpressionPlugin)
            # can reach the same instance via get_karada_state_server()
            set_karada_state_server(self.animation_handler)
            # Preload idle animations in background (non-blocking)
            try:
                asyncio.create_task(self.animation_handler.ensure_idle_preloaded())
            except Exception:
                pass
            # Mount the public Karada REST + WS API router
            try:
                from core.karada_api import create_karada_router

                karada_router, karada_ws_router = create_karada_router(
                    self.animation_handler
                )
                self.app.include_router(karada_router)
                self.app.include_router(karada_ws_router)
                log_info(f"{LOG_PREFIX} Karada API router mounted at /api/karada/")
            except Exception as karada_exc:
                log_warning(
                    f"{LOG_PREFIX} Karada API router failed to mount (non-fatal): {karada_exc}"
                )
        except Exception as e:
            # If KaradaStateServer fails to initialize, create a lightweight stub
            log_warning(f"{LOG_PREFIX} KaradaStateServer init failed: {e}")

            class _AnimStub:
                def __init__(self):
                    self._current_animation_file = None
                    self._current_animation_descriptor = None
                    self._current_animation_started_at = None

                def get_current_animation_state(self):
                    return {
                        "state": AnimationState.IDLE.value,
                        "animation_file": None,
                        "descriptor": None,
                    }

            self.animation_handler = _AnimStub()

        # Register as an interface only when autostart is enabled.
        # Tests/dev harnesses may instantiate the WebUI with autostart disabled
        # and without a fully initialized core initializer.
        if self.autostart:
            try:
                register_interface(INTERFACE_NAME, self)
                log_info(f"{LOG_PREFIX} Interface registered", log_file=WEBUI_LOG)
            except Exception as exc:
                log_warning(
                    f"{LOG_PREFIX} Interface registration failed (non-fatal): {exc}",
                    log_file=WEBUI_LOG,
                )

        # Initialize global action state manager
        self.action_state_manager = get_action_state_manager()
        # Register callback to broadcast state changes to all WebSocket clients
        self.action_state_manager.register_state_changed_callback(
            self._broadcast_action_state
        )
        log_info(
            f"{LOG_PREFIX} Action state manager initialized with WebSocket broadcast",
            log_file=WEBUI_LOG,
        )

        # Persona manager will be initialized in start() method after core initialization
        self.persona_manager = None

        # Plugin extension points: JS content and API route handlers registered by plugins.
        # Plugins call register_plugin_js() / register_plugin_api_route() from their start().
        # Removing a plugin simply leaves these dicts empty — no core traces remain.
        self._plugin_scripts: dict[str, str] = {}
        self._plugin_api_routes: dict[str, Any] = {}
        # Plugin section tabs: keyed by section name, each entry is a list of
        # {"tab_id": str, "button_html": str, "panel_html": str} dicts.
        self._plugin_section_tabs: dict[str, list[dict[str, str]]] = {}

        if self.autostart:
            log_info(
                f"{LOG_PREFIX} Autostart enabled - will start server when event loop is available",
                log_file=WEBUI_LOG,
            )
            # Don't start server here - it will be started by the main application
        else:
            log_info(
                f"{LOG_PREFIX} Autostart disabled - {BRAND_NAME} will not start automatically",
                log_file=WEBUI_LOG,
            )

        # Attempt to initialize the chat_archives DB table in background (best-effort)
        try:
            from core.chat_archives_db import init_chat_archives_table

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(init_chat_archives_table())
                else:
                    loop.run_until_complete(init_chat_archives_table())
            except Exception:
                # Fallback: ignore - the endpoints will try initializing on demand
                pass
        except Exception:
            pass

        if self.autostart:
            self._schedule_uploads_cleanup()

        self.app.get("/")(self.index)
        self.app.get("/health")(self.health)
        self.app.get("/api/action-state")(self.get_action_state_endpoint)
        self.app.get("/api/emotion-state")(self.get_emotion_state_endpoint)
        self.app.get("/stats")(self.stats)
        self.app.get("/logs")(self.logs_page)
        self.app.get("/diary")(self.diary_page)
        self.app.post("/api/log-console")(self.log_console_endpoint)
        self.app.websocket("/ws")(self.websocket_endpoint)
        self.app.websocket("/logs")(self.logs_ws_endpoint)
        # Endpoints the stage/legacy clients hit outside /api/karada/* but that
        # act on the avatar or feed text into the chain — gated by the same
        # optional SYNTH_WEBUI_API_TOKEN as /ws and the Karada REST router
        # (no-op when the token is unset). Per-route Depends is safe here; the
        # router-level-dependency-on-websocket bug only affects WS routes.
        from core.karada_api import _require_api_token

        # Auris audio endpoints
        self.app.post("/api/audio/upload", dependencies=[Depends(_require_api_token)])(
            self.audio_upload_endpoint
        )
        self.app.post("/api/chat/attachments")(self.chat_attachment_upload_endpoint)
        # helper endpoint for Vosk language selection (legacy compat, delegates to MODEL_MANAGER)
        self.app.post("/api/auris/vosk/download")(self.vosk_model_download)
        # Model management endpoints (SSOT: MODEL_MANAGER)
        self.app.get("/api/models")(self.list_models)
        self.app.get("/api/models/{model_id}")(self.get_model_detail)
        self.app.post("/api/models/{model_id}/download")(self.start_model_download)
        self.app.get("/api/models/{model_id}/progress")(self.get_model_progress)
        self.app.delete("/api/models/{model_id}")(self.delete_model)
        self.app.get("/api/models/{model_id}/sample/{voice}")(self.model_sample_voice)
        self.app.get("/api/models/{model_id}/voice/{voice}/exists")(
            self.model_voice_sample_exists
        )
        self.app.post("/api/models/{model_id}/voice/{voice}/generate")(
            self.model_generate_voice_sample
        )
        self.app.websocket("/api/audio/stream")(self.audio_stream_ws_endpoint)

        # Vox metadata/sample endpoints
        self.app.get("/api/vox/speakers")(self.vox_speakers)
        self.app.get("/api/vox/sample")(self.vox_sample)
        self.app.get("/api/vrm")(self.list_vrm_models)
        self.app.get("/api/vrm/active")(self.get_active_vrm_endpoint)
        self.app.post("/api/vrm")(self.upload_vrm_model)
        self.app.post("/api/vrm/active")(self.set_active_vrm_endpoint)
        self.app.delete("/api/vrm/{model_name}")(self.delete_vrm_model)

        self.app.post("/api/persona")(self.upload_persona_pack)
        # Skins management endpoints
        self.app.get("/api/skins")(self.list_skins)
        # new helper: allow clients to query which skin is active
        self.app.get("/api/skins/current_skin")(self.get_current_skin)
        self.app.post(
            "/api/skins/{skin_name}/activate",
            dependencies=[Depends(_require_api_token)],
        )(self.activate_skin)
        self.app.post("/api/skins/uploaded/clear")(self.clear_uploaded_vrm)
        # Skin editor endpoints
        self.app.post("/api/skins")(self.create_skin)
        self.app.post("/api/skins/upload")(self.upload_skin_zip)
        self.app.post("/api/skins/{skin_name}/vrm")(self.upload_skin_vrm)
        self.app.post("/api/skins/{skin_name}/preview")(self.upload_skin_preview)
        self.app.get("/api/skins/{skin_name}/download")(self.download_skin)
        self.app.delete("/api/skins/{skin_name}")(self.delete_skin)
        self.app.get("/api/components")(self.components_summary)
        self.app.post("/api/components/reload")(self.reload_component)
        self.app.post("/api/components/dev/toggle")(self.toggle_dev_components)
        self.app.post("/api/system/restart")(self.restart_system)
        self.app.get("/api/config")(self.config_summary)
        # File-backed exposed variables: upload/download handlers
        self.app.post("/api/config/{key}/upload")(self.upload_exposed_file)
        self.app.get("/api/config/{key}/file")(self.get_exposed_file)
        # Debug endpoints (only enabled when WEB_DEBUG=1)
        self.app.get("/api/debug/db_pool")(self.db_pool_debug)
        self.app.post("/api/debug/inject_message")(self.debug_inject_message)
        self.app.post("/api/debug/tts_test")(self.debug_tts_test)
        self.app.get("/api/debug/expressions")(self.debug_expressions)
        self.app.post("/api/config")(self.update_config_entry)
        # Cortex-aware endpoints
        self.app.post("/api/components/cortex")(self.set_cortex_engine)
        # Login control for Selenium-based engines
        self.app.post("/api/components/cortex/login")(self.cortex_login)
        # Model selection for cortex engines
        self.app.post("/api/components/cortex/model")(self.set_cortex_model)
        # Model selection for external Vox / Auris engines (persist the chosen
        # model into the endpoint's extra_config so the bridge picks it up).
        self.app.post("/api/components/vox/model")(self.set_vox_model)
        self.app.post("/api/components/auris/model")(self.set_auris_model)
        # Run component actions on demand (e.g., Run Now button)
        self.app.post("/api/components/run")(self.run_component)
        self.app.get("/api/logchat/info")(self.get_logchat_info)
        self.app.get("/api/about")(self.about_summary)
        self.app.get("/api/diary")(self.diary_summary)
        self.app.post("/api/diary/archive")(self.archive_diary_entries)
        self.app.post("/api/diary/unarchive")(self.unarchive_diary_entries)
        self.app.delete("/api/diary/archive")(self.delete_archived_entries)
        # Chat archive API (filesystem-backed)
        self.app.post("/api/chat/archive")(self.archive_chat)
        self.app.get("/api/chat/archives")(self.list_chat_archives)
        self.app.get("/api/chat/archives/{archive_id}")(self.get_chat_archive)
        self.app.post("/api/chat/restore")(self.restore_chat_archive)
        self.app.delete("/api/chat/archives/{archive_id}")(self.delete_chat_archive)
        self.app.post("/api/chat/archives/{archive_id}/rename")(
            self.rename_chat_archive
        )
        self.app.post("/api/chat/session_meta")(self.set_session_meta)
        self.app.get("/api/chat/session_meta")(self.get_session_meta)
        # History API endpoints (unified diary, grillo, chat history)
        self.app.get("/api/history/diary")(self.history_diary)
        self.app.get("/api/history/grillo")(self.history_grillo)
        self.app.get("/api/history/calendar")(self.history_calendar)
        self.app.post("/api/history/calendar")(self.create_calendar_event)
        self.app.put("/api/history/calendar/{event_id}")(self.update_calendar_event)
        self.app.delete("/api/history/calendar/{event_id}")(self.delete_calendar_event)
        self.app.get("/api/history/calendar/upcoming")(self.history_calendar_upcoming)
        self.app.get("/calendar.ics")(self.export_calendar_ics)
        # External calendar subscriptions (CalDAV / ICS)
        self.app.get("/api/history/calendar/external")(self.list_external_calendars)
        self.app.post("/api/history/calendar/external")(self.add_external_calendar)
        self.app.delete("/api/history/calendar/external/{calendar_id}")(
            self.delete_external_calendar
        )
        self.app.get("/api/history/dreams")(self.history_dreams)
        self.app.get("/api/history/interface-paths")(self.list_known_interface_paths)
        self.app.get("/api/history/chat")(self.history_chat)
        self.app.get("/api/log-failures")(self.list_log_failures)
        self.app.delete("/api/log-failures/{failure_id}")(self.delete_log_failure)

        # Agent tasks endpoints (Agent Loop persistence & control)
        self.app.get("/api/agent/tasks")(self.list_agent_tasks)
        self.app.get("/api/agent/tasks/{task_id}")(self.get_agent_task)
        self.app.post("/api/agent/tasks")(self.create_agent_task)
        self.app.post("/api/agent/tasks/{task_id}/pause")(self.pause_agent_task)
        self.app.post("/api/agent/tasks/{task_id}/resume")(self.resume_agent_task)
        self.app.post("/api/agent/tasks/{task_id}/cancel")(self.cancel_agent_task)
        # Agent proposal approval endpoint
        self.app.get("/api/agent/proposals")(self.list_agent_proposals)
        self.app.post("/api/agent/proposals/{proposal_id}/approve")(
            self.approve_agent_proposal
        )
        self.app.get("/api/animations/{skin}/{animation_type}")(
            self.get_animations_for_type
        )
        self.app.get(
            "/api/skins/{skin}/animations/{animation_type}/{animation_file}.json"
        )(self.get_animation_descriptor)
        self.app.get("/service-worker.js")(self.service_worker)
        self.app.get("/favicon.ico")(self.favicon)
        self.app.post("/api/animations/upload")(self.upload_animation)
        self.app.get("/api/animations/uploads")(self.list_animation_uploads)
        self.app.delete("/api/animations/uploads/{upload_id}")(
            self.delete_animation_upload
        )
        self.app.post("/api/animations/promote")(self.promote_animation_upload)
        self.app.get("/api/prompt_override")(self.get_prompt_override)
        # Generic integrations endpoints (source-agnostic)
        self.app.post("/api/integrations/messages")(self.post_integration_message)
        self.app.get("/api/integrations/outbox")(self.get_integration_outbox)

        # Provide an internal endpoint implementation that doesn't rely on a
        # bound `get_animation_state` method at init time. This avoids
        # AttributeError in environments with dynamic reloads.
        async def _animation_state_endpoint():
            try:
                if not getattr(self, "animation_handler", None):
                    return JSONResponse(
                        {"state": "idle", "descriptor": None, "started_at": None}
                    )

                current = self.animation_handler.get_current_animation_state()
                return JSONResponse(
                    {
                        "state": current.get("state"),
                        "descriptor": current.get("descriptor"),
                        "started_at": current.get("started_at"),
                    }
                )
            except Exception as exc:
                log_error(f"{LOG_PREFIX} animation_state endpoint failed: {exc}")
                raise HTTPException(
                    status_code=500, detail=f"Failed to retrieve animation state: {exc}"
                ) from exc

        self.app.get("/api/animation_state")(_animation_state_endpoint)
        # Allow authorized clients to request a centralized state change
        self.app.post("/api/animation_state")(self.set_animation_state)
        # Emotion state endpoint (separate from animation_state) — returns current emotion intensities
        self.app.get("/api/emotion_state")(self.get_emotion_state_endpoint)
        self.app.get("/api/locations")(self.get_suggested_locations)
        self.app.get("/api/weather/current")(self.get_current_weather_endpoint)

        # External endpoints (custom AI service connections)
        self.app.get("/api/external-endpoints")(self.list_external_endpoints)
        self.app.post("/api/external-endpoints")(self.create_external_endpoint)
        # NOTE: /presets MUST be registered before /{ep_id} to avoid routing conflicts
        self.app.get("/api/external-endpoints/presets")(
            self.list_external_endpoint_presets
        )
        self.app.get("/api/external-endpoints/{ep_id}")(self.get_external_endpoint)
        self.app.put("/api/external-endpoints/{ep_id}")(self.update_external_endpoint)
        self.app.delete("/api/external-endpoints/{ep_id}")(
            self.delete_external_endpoint
        )
        self.app.post("/api/external-endpoints/{ep_id}/probe")(
            self.probe_external_endpoint
        )
        self.app.post("/api/external-endpoints/{ep_id}/ping")(
            self.ping_external_endpoint
        )
        self.app.post("/api/external-endpoints/{ep_id}/enable")(
            self.enable_external_endpoint
        )
        self.app.post("/api/external-endpoints/{ep_id}/disable")(
            self.disable_external_endpoint
        )
        self.app.put("/api/external-endpoints/{ep_id}/mapping")(
            self.set_external_endpoint_mapping
        )
        self.app.put("/api/external-endpoints/{ep_id}/model")(
            self.set_external_endpoint_model
        )
        self.app.post("/api/database/backup")(self.create_database_backup_endpoint)

        # Template sections route for modular loading
        self.app.get("/templates/{section}.html")(self.serve_template_section)
        # Endpoint serving an iframe host page for embedding sections inside an iframe
        self.app.get("/iframe/{section}")(self.iframe_host)

        # Plugin dispatch middleware — handles JS files and API routes registered by
        # plugins at runtime.  Added last so it runs first in the middleware stack,
        # intercepting plugin-specific paths before the static-file mounts.
        try:
            from starlette.middleware.base import BaseHTTPMiddleware

            class _PluginDispatchMiddleware(BaseHTTPMiddleware):
                async def dispatch(inner_self, request, call_next):
                    path = request.url.path

                    # Serve registered plugin JS: GET /js/plugins/<name>.js
                    if path.startswith("/js/plugins/") and path.endswith(".js"):
                        plugin_name = path[len("/js/plugins/") : -len(".js")]
                        content = self._plugin_scripts.get(plugin_name)
                        if content is not None:
                            from starlette.responses import Response

                            return Response(
                                content,
                                media_type="application/javascript",
                            )

                    # Dispatch registered plugin API routes
                    handler = self._plugin_api_routes.get(path)
                    if handler is not None:
                        import inspect
                        from starlette.responses import JSONResponse
                        from starlette.responses import Response as StarletteResponse

                        sig = inspect.signature(handler)
                        result = handler(request) if sig.parameters else handler()
                        if inspect.isawaitable(result):
                            result = await result
                        if isinstance(result, StarletteResponse):
                            return result
                        return JSONResponse(result)

                    return await call_next(request)

            self.app.add_middleware(_PluginDispatchMiddleware)
        except Exception as _mw_exc:
            log_warning(
                f"{LOG_PREFIX} Failed to add plugin dispatch middleware: {_mw_exc}"
            )

    # ------------------------------------------------------------------
    # Plugin extension API
    # ------------------------------------------------------------------

    def register_plugin_js(self, name: str, js_content: str) -> None:
        """Register a plugin's JS content to be served at /js/plugins/<name>.js.

        The script tag ``<script src="/js/plugins/<name>.js" defer></script>`` is
        injected automatically into every rendered index page.  Calling this
        multiple times for the same *name* replaces the previous content.
        """
        self._plugin_scripts[name] = js_content
        log_info(
            f"{LOG_PREFIX} Plugin JS registered: '{name}' ({len(js_content)} bytes)",
            log_file=WEBUI_LOG,
        )

    def register_plugin_api_route(self, path: str, handler: Any) -> None:
        """Register an async or sync callable at *path* for GET requests.

        The handler must return a JSON-serialisable value.  Registering the
        same path again replaces the previous handler.
        """
        self._plugin_api_routes[path] = handler
        log_info(
            f"{LOG_PREFIX} Plugin API route registered: {path}",
            log_file=WEBUI_LOG,
        )

    def register_plugin_section_tab(
        self,
        section: str,
        tab_id: str,
        button_html: str,
        panel_html: str,
    ) -> None:
        """Register a sub-tab to be injected into a section template.

        When ``/templates/<section>.html`` is served, the *button_html* snippet
        is appended inside the ``<nav class="sub-nav">`` element and *panel_html*
        is appended inside the ``.sub-tabs-container`` element.

        Calling this multiple times with the same *tab_id* replaces the previous
        registration.  Removing a plugin simply leaves the dict empty.
        """
        tabs = self._plugin_section_tabs.setdefault(section, [])
        # Replace existing entry for this tab_id
        self._plugin_section_tabs[section] = [t for t in tabs if t["tab_id"] != tab_id]
        self._plugin_section_tabs[section].append(
            {"tab_id": tab_id, "button_html": button_html, "panel_html": panel_html}
        )
        log_info(
            f"{LOG_PREFIX} Plugin section tab registered: section='{section}' tab_id='{tab_id}'",
            log_file=WEBUI_LOG,
        )

    def _is_missing_agent_table_error(self, exc: Exception) -> bool:
        """Return True when agent tables are missing so endpoints can degrade gracefully."""
        msg = str(exc).lower()
        return (
            "no such table" in msg
            or "doesn't exist" in msg
            or "does not exist" in msg
            or "unknown table" in msg
            or "undefinedtable" in msg
        )

    # --- Agent endpoints ---
    async def list_agent_tasks(self, limit: int = 50):
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, engine, status, created_at, updated_at FROM agent_tasks ORDER BY created_at DESC LIMIT %s",
                        (int(limit),),
                    )
                    rows = await cur.fetchall()
                    tasks = []
                    for r in rows:
                        tasks.append(
                            {
                                "id": r[0],
                                "engine": r[1],
                                "status": r[2],
                                "created_at": r[3].isoformat() if r[3] else None,
                                "updated_at": r[4].isoformat() if r[4] else None,
                            }
                        )
                    return JSONResponse({"tasks": tasks})
        except Exception as e:
            if self._is_missing_agent_table_error(e):
                log_warning(
                    f"{LOG_PREFIX} list_agent_tasks: agent_tasks table missing, returning empty list"
                )
                return JSONResponse({"tasks": []})
            error_msg = str(e)
            log_error(f"{LOG_PREFIX} list_agent_tasks failed: {error_msg}")
            raise HTTPException(status_code=500, detail=error_msg)

    async def get_agent_task(self, task_id: int):
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, engine, status, input, iterations_meta, output, trainer_id, metadata, created_at, updated_at FROM agent_tasks WHERE id=%s",
                        (int(task_id),),
                    )
                    row = await cur.fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail="task not found")
                    return JSONResponse(
                        {
                            "id": row[0],
                            "engine": row[1],
                            "status": row[2],
                            "input": json.loads(row[3]) if row[3] else None,
                            "iterations_meta": json.loads(row[4]) if row[4] else [],
                            "output": json.loads(row[5]) if row[5] else None,
                            "trainer_id": row[6],
                            "metadata": json.loads(row[7]) if row[7] else None,
                            "created_at": row[8].isoformat() if row[8] else None,
                            "updated_at": row[9].isoformat() if row[9] else None,
                        }
                    )
        except HTTPException:
            raise
        except Exception as e:
            error_msg = str(e)
            log_error(f"{LOG_PREFIX} get_agent_task failed: {error_msg}")
            raise HTTPException(status_code=500, detail=error_msg)

    async def create_agent_task(self, request: Request):
        try:
            body = await request.json()
            engine = body.get("engine", "default")
            input_payload = body.get("input") or body.get("prompt") or {}
            max_iterations = body.get("max_iterations")

            # Check agent enabled
            from core.config_manager import config_registry as cfg

            if not bool(cfg.get_var("AGENT_ENABLED", True)):
                raise HTTPException(status_code=403, detail="Agent disabled")

            from core.agent_core import get_agent_loop_manager

            manager = get_agent_loop_manager()
            task_id = await manager.run_loop(
                engine=engine,
                input_payload=input_payload,
                context={},
                max_iterations=max_iterations,
            )
            if not task_id:
                raise HTTPException(
                    status_code=500, detail="Failed to create agent task"
                )
            return JSONResponse({"task_id": task_id})
        except HTTPException:
            raise
        except Exception as e:
            error_msg = str(e)
            log_error(f"{LOG_PREFIX} create_agent_task failed: {error_msg}")
            raise HTTPException(status_code=500, detail=error_msg)

    async def create_database_backup_endpoint(self):
        try:
            from core.db_backup import create_database_backup

            backup_path = await create_database_backup(
                reason="manual_webui",
                force=True,
            )
            if backup_path is None:
                raise HTTPException(
                    status_code=500,
                    detail="Manual database backup did not produce an output file",
                )
            return JSONResponse(
                {
                    "success": True,
                    "path": str(backup_path),
                    "filename": backup_path.name,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            error_msg = str(e)
            log_error(
                f"{LOG_PREFIX} create_database_backup_endpoint failed: {error_msg}"
            )
            raise HTTPException(status_code=500, detail=error_msg)

    async def pause_agent_task(self, task_id: int):
        try:
            from core.agent_core import get_agent_loop_manager

            manager = get_agent_loop_manager()
            manager.pause_task(int(task_id))
            # Persist status
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE agent_tasks SET status=%s WHERE id=%s",
                        ("paused", int(task_id)),
                    )
                    await conn.commit()
            return JSONResponse({"status": "paused"})
        except Exception as e:
            error_msg = str(e)
            log_error(f"{LOG_PREFIX} pause_agent_task failed: {error_msg}")
            raise HTTPException(status_code=500, detail=error_msg)

    async def resume_agent_task(self, task_id: int):
        try:
            from core.agent_core import get_agent_loop_manager

            manager = get_agent_loop_manager()
            manager.resume_task(int(task_id))
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE agent_tasks SET status=%s WHERE id=%s",
                        ("running", int(task_id)),
                    )
                    await conn.commit()
            return JSONResponse({"status": "running"})
        except Exception as e:
            error_msg = str(e)
            log_error(f"{LOG_PREFIX} resume_agent_task failed: {error_msg}")
            raise HTTPException(status_code=500, detail=error_msg)

    async def cancel_agent_task(self, task_id: int):
        try:
            from core.agent_core import get_agent_loop_manager

            manager = get_agent_loop_manager()
            manager.cancel_task(int(task_id))
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE agent_tasks SET status=%s WHERE id=%s",
                        ("cancelled", int(task_id)),
                    )
                    await conn.commit()
            return JSONResponse({"status": "cancelled"})
        except Exception as e:
            error_msg = str(e)
            log_error(f"{LOG_PREFIX} cancel_agent_task failed: {error_msg}")
            raise HTTPException(status_code=500, detail=error_msg)

    async def approve_agent_proposal(self, proposal_id: int, request: Request):
        try:
            body = await request.json()
            trainer = body.get("trainer") or body.get("trainer_id") or None
            original_message = {"sender_id": trainer} if trainer else None

            from core.core_initializer import PLUGIN_REGISTRY

            plugin = PLUGIN_REGISTRY.get("agent")
            if not plugin:
                raise HTTPException(status_code=404, detail="Agent plugin not loaded")

            res = await plugin.execute_action(
                {
                    "type": "approve_action",
                    "payload": {"proposal_id": int(proposal_id)},
                },
                {},
                None,
                original_message,
            )
            return JSONResponse({"result": res})
        except HTTPException:
            raise
        except Exception as e:
            error_msg = str(e)
            log_error(f"{LOG_PREFIX} approve_agent_proposal failed: {error_msg}")
            raise HTTPException(status_code=500, detail=error_msg)

    async def list_agent_proposals(self, limit: int = 50):
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, command, proposer, status, request_ts FROM agent_activity_log WHERE status=%s ORDER BY request_ts DESC LIMIT %s",
                        ("proposed", int(limit)),
                    )
                    rows = await cur.fetchall()
                    proposals = []
                    for r in rows:
                        proposals.append(
                            {
                                "id": r[0],
                                "command": r[1],
                                "proposer": r[2],
                                "status": r[3],
                                "requested_at": r[4].isoformat() if r[4] else None,
                            }
                        )
                    return JSONResponse({"proposals": proposals})
        except Exception as e:
            if self._is_missing_agent_table_error(e):
                log_warning(
                    f"{LOG_PREFIX} list_agent_proposals: agent_activity_log table missing, returning empty list"
                )
                return JSONResponse({"proposals": []})
            error_msg = str(e)
            log_error(f"{LOG_PREFIX} list_agent_proposals failed: {error_msg}")
            raise HTTPException(status_code=500, detail=error_msg)

    async def set_animation_state(self, request: Request):
        """Set the centralized animation state. Expected JSON:
        {"state": "think|write|idle|talk|touch", "session_id": "...", "loop": true}
        """
        data = await request.json()
        state_str = data.get("state") if isinstance(data, dict) else None
        if not state_str or not isinstance(state_str, str):
            raise HTTPException(
                status_code=400, detail="'state' is required and must be a string"
            )
        try:
            state_enum = AnimationState[state_str.upper()]
        except Exception:
            raise HTTPException(
                status_code=400, detail=f"Unknown animation state: {state_str}"
            )

        session_id = data.get("session_id")
        loop = bool(data.get("loop", True))
        context_id = data.get("context_id")
        source = data.get("source")
        priority = data.get("priority")
        if priority is not None:
            try:
                priority = int(priority)
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail="'priority' must be an integer"
                ) from exc

        if state_enum == AnimationState.TOUCH:
            if "loop" not in data:
                loop = False
            if not context_id:
                context_id = _WEBUI_TOUCH_CONTEXT_ID
            if priority is None:
                priority = _WEBUI_TOUCH_PRIORITY
            if not source:
                source = "webui.touch"

        try:
            await self.animation_handler.play_animation(
                state_enum,
                session_id=session_id,
                loop=loop,
                context_id=context_id,
                priority=priority,
                source=source,
            )
            return JSONResponse(
                {"status": "ok", "state": state_str, "session_id": session_id}
            )
        except Exception as exc:
            log_error(f"{LOG_PREFIX} set_animation_state failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def _handle_touch_animation_request(
        self, session_id: str, payload: Dict[str, Any]
    ) -> None:
        """Convert a WebUI touch interaction into an authoritative Karada state."""
        if not self.animation_handler:
            return

        source = payload.get("source") or "webui.touch"
        priority = payload.get("priority")
        if not isinstance(priority, int):
            priority = _WEBUI_TOUCH_PRIORITY

        context_id = payload.get("context_id") or _WEBUI_TOUCH_CONTEXT_ID
        touched_part = payload.get("mapped_part") or payload.get("part") or "unknown"
        log_info(
            f"{LOG_PREFIX} Touch interaction from session {session_id}: part={touched_part}, "
            f"context={context_id}, priority={priority}"
        )

        await self.animation_handler.play_animation(
            AnimationState.TOUCH,
            session_id=None,
            loop=False,
            context_id=str(context_id),
            priority=int(priority),
            source=str(source),
        )

        # Record the touch on the Synth avatar as a Karada interaction event.
        try:
            from core.karada_touch_events import record_touch_event, EVENT_SYNTH_TOUCH

            # Prefer the precise catalog zone id resolved by the frontend 3D
            # zoning; fall back to the raw node name / heuristic label.
            raw_part = (
                payload.get("precise_id")
                or payload.get("part")
                or payload.get("mapped_part")
            )
            await record_touch_event(
                session_id=session_id,
                interface_path=f"{INTERFACE_NAME}/{session_id}",
                event_type=EVENT_SYNTH_TOUCH,
                raw_part=str(raw_part) if raw_part and raw_part != "unknown" else None,
                username=self._get_session_username(),
            )
        except Exception as rec_exc:
            log_debug(f"{LOG_PREFIX} Failed to record synth_touch event: {rec_exc}")

    def _get_session_username(self) -> str:
        """Best-effort display name for the interacting WebUI user."""
        try:
            from core.config import TRAINER_NAME

            if TRAINER_NAME and TRAINER_NAME != "Trainer":
                return str(TRAINER_NAME)
        except Exception:
            pass
        return "Trainer"

    async def _handle_interaction_event(
        self, session_id: str, payload: Dict[str, Any]
    ) -> None:
        """Record a non-avatar 3D interaction (environment or window tap)."""
        try:
            from core.karada_touch_events import (
                record_touch_event,
                EVENT_ENVIRONMENT_TAP,
                EVENT_WINDOW_TAP,
            )

            subtype = payload.get("subtype") or payload.get("interaction")
            if subtype == "window_tap":
                event_type = EVENT_WINDOW_TAP
            else:
                event_type = EVENT_ENVIRONMENT_TAP

            await record_touch_event(
                session_id=session_id,
                interface_path=f"{INTERFACE_NAME}/{session_id}",
                event_type=event_type,
                username=self._get_session_username(),
            )
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} Failed to record interaction event: {exc}")

    # ------------------------------------------------------------------
    # Interface metadata
    # ------------------------------------------------------------------
    @staticmethod
    def get_interface_id() -> str:
        return INTERFACE_NAME

    @staticmethod
    def get_supported_actions() -> dict:
        from plugins.vox_plugin import is_vox_enabled

        vox_on = is_vox_enabled()

        webui_optional = ["interface_path"]
        webui_description = f"Send a reply to a {BRAND_NAME} session."
        if vox_on:
            webui_optional.append("send_as_voice")
            webui_description += (
                " send_as_voice defaults to false. Only set send_as_voice=true when "
                "the user explicitly asked for a voice/audio reply, or when they "
                "just spoke to you by voice. Otherwise reply as plain text."
            )

        return {
            "message_synth_webui": {
                "required_fields": ["text"],
                "optional_fields": webui_optional,
                "description": webui_description,
            },
            "message_mate_engine": {
                "required_fields": ["text"],
                "optional_fields": ["target"],
                "description": "(Deprecated) Send a text message to an integration outbox (source: mate).",
            },
            "message_integration": {
                "required_fields": ["source", "text"],
                "optional_fields": ["target"],
                "description": "Send a text message to a named integration outbox (generic).",
            },
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> dict:
        if action_name == "message_synth_webui":
            from plugins.vox_plugin import is_vox_enabled

            vox_on = is_vox_enabled()

            payload = {
                "text": {
                    "type": "string",
                    "example": "Ciao!",
                    "description": "Message content to deliver",
                },
                "target": {
                    "type": "string",
                    "example": "session-id",
                    "description": "Session identifier returned by the websocket",
                },
            }
            if vox_on:
                payload["send_as_voice"] = {
                    "type": "boolean",
                    "example": True,
                    "description": "Optional, defaults to false. When true, your 'text' is synthesised and the avatar speaks it aloud (with the text shown as the caption bubble). Voice synthesis is slow, so use it SPARINGLY: only set it when the user EXPLICITLY asked to be answered with voice/audio (in any language), or when the user just spoke to you by voice. Do NOT set it just because it might be nice. For every ordinary reply, leave it out (or false) and answer as plain text.",
                    "optional": True,
                }

            return {
                "description": f"Send a message to the {BRAND_NAME} browser client.",
                "payload": payload,
            }
        if action_name == "message_mate_engine":
            return {
                "description": "(Deprecated) Send a message to an integration outbox (legacy mate alias).",
                "payload": {
                    "text": {
                        "type": "string",
                        "example": "Hello from SyntH!",
                        "description": "Message content to deliver",
                    },
                    "target": {
                        "type": "string",
                        "example": "mate-session-id",
                        "description": "Optional target/session identifier",
                        "optional": True,
                    },
                },
            }
        if action_name == "message_integration":
            return {
                "description": "Send a message to a named integration outbox.",
                "payload": {
                    "source": {
                        "type": "string",
                        "description": "Integration source name (e.g., 'mate')",
                    },
                    "text": {"type": "string", "example": "Hello from SyntH!"},
                    "target": {
                        "type": "string",
                        "example": "session-id",
                        "optional": True,
                    },
                },
            }
        return {}

    @staticmethod
    def get_interface_instructions() -> str:
        return (
            f"Use interface: {INTERFACE_NAME} to converse through the {BRAND_NAME} browser UI. "
            "The target field must contain the session identifier emitted by the "
            "websocket handshake."
        )

    # ------------------------------------------------------------------
    # HTTP Handlers
    # ------------------------------------------------------------------
    def _render_index(self) -> str:
        """Render the main web UI page from template."""
        try:
            # Read the template file - use the shell template that loads modular sections
            template_path = (
                Path(__file__).parent / "webui_templates" / "synth_webui_shell.html"
            )
            with open(template_path, "r", encoding="utf-8") as f:
                template = f.read()

            # Get replacement values
            from core.message_chain import RESPONSE_TIMEOUT, get_failed_message_text

            synth_name = str(config_registry.get_value("SYNTH_NAME", "SyntH"))
            replacements = {
                "%%BRAND_NAME%%": BRAND_NAME,
                "%%SYNTH_NAME%%": synth_name,
                "%%LOGO_URL%%": str(
                    getattr(self, "logo_url", "/static/synth_logo_bg.png")
                ),
                "%%RESPONSE_TIMEOUT%%": str(int(RESPONSE_TIMEOUT)),
                "%%FAILED_MESSAGE_TEXT%%": str(get_failed_message_text()),
                # Expose WEB_DEBUG flag to the template (default false)
                "%%WEB_DEBUG%%": "1"
                if os.getenv("WEB_DEBUG", "0") in ("1", "true", "True")
                else "0",
                # Chat resizable flag (configurable via exposed variable)
                "%%CHAT_RESIZABLE%%": "true"
                if str(self._get_chat_resizable()).lower() in ("1", "true", "yes")
                else "false",
                "%%MULTI_SESSION%%": "true"
                if self._multi_session_enabled()
                else "false",
            }

            # Vox (TTS) flag exposed to the WebUI client is derived from
            # which engine is active; a value of "disabled" means off.
            try:
                active_vox = str(
                    config_registry.get_value(
                        "ACTIVE_VOX_ENGINE",
                        "",
                        value_type=str,
                        group="plugins",
                        component="vox_plugin",
                    )
                )
                _vox_enabled = bool(active_vox and active_vox != "disabled")
            except Exception:
                _vox_enabled = False
            try:
                _vox_cache = int(
                    config_registry.get_value(
                        "VOX_AUDIO_CACHE_SIZE",
                        40,
                        value_type=int,
                        group="plugins",
                        component="vox_plugin",
                    )
                )
            except Exception:
                _vox_cache = 40
            replacements["%%VOX_ENABLED%%"] = "true" if _vox_enabled else "false"
            replacements["%%VOX_AUDIO_CACHE_SIZE%%"] = str(_vox_cache)

            # Iris enabled flag exposed to the WebUI client. Attachments require
            # the Iris subsystem to be available in the current session.
            try:
                active_iris = str(
                    config_registry.get_value(
                        "ACTIVE_IRIS_ENGINE",
                        "disabled",
                        label="Active Iris Engine",
                        description="Name of the active Iris vision engine. Set to 'disabled' to turn off the Iris subsystem.",
                        value_type=str,
                        group="plugins",
                        component="iris_plugin",
                    )
                )
                _iris_enabled = bool(active_iris and active_iris != "disabled")
            except Exception:
                _iris_enabled = False
            replacements["%%IRIS_ENABLED%%"] = "true" if _iris_enabled else "false"

            # Accent color config + presets (exposed to client as runtime config)
            try:
                accent = str(config_registry.get_value("WEBUI_ACCENT_COLOR", "#6bfefe"))
            except Exception:
                accent = "#6bfefe"
            presets = ["#6bfefe", "#ff6bd6", "#18c98c", "#ffd166", "#ff9ecb"]
            replacements["%%WEBUI_ACCENT_COLOR%%"] = accent
            import json

            replacements["%%WEBUI_ACCENT_PRESETS%%"] = json.dumps(presets)
            # always include a cache‑busting token for static assets
            # use a timestamp so the value changes on each render
            import time

            replacements["%%STATIC_VERSION%%"] = str(int(time.time()))

            for placeholder, value in replacements.items():
                template = template.replace(placeholder, value)

            # Inject script tags for registered plugins right before </body>.
            # _plugin_scripts is populated lazily when plugins call
            # register_plugin_js(); re-reading per request is intentional (no cache).
            if self._plugin_scripts:
                extra = "".join(
                    f'<script src="/js/plugins/{name}.js" defer></script>\n'
                    for name in self._plugin_scripts
                )
                template = template.replace("</body>", f"{extra}</body>", 1)

            return template

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to render index template: {exc}")
            # Fallback to a simple error page
            return f"""
<!DOCTYPE html>
<html>
<head><title>Error</title></head>
<body>
<h1>Synthetic Heart Error</h1>
<p>Failed to load the web interface: {exc}</p>
</body>
</html>
"""

    async def index(self):
        log_info(f"{LOG_PREFIX} Index route called")
        try:
            html = self._render_index()
            log_info(f"{LOG_PREFIX} Rendered HTML length: {len(html)}")
            # Return the rendered HTML as an HTMLResponse. Keep this inside
            # the try-block so exceptions during rendering lead to a 500 and
            # we never fall through returning None.
            return HTMLResponse(content=html, media_type="text/html")
        except Exception as exc:
            log_error(f"{LOG_PREFIX} failed to render index: {exc}")
            raise HTTPException(
                status_code=500, detail="Unable to render Synthetic Heart"
            ) from exc

    def _get_chat_resizable(self) -> bool:
        """Return whether chat should be resizable (from config/DB)."""
        try:
            from core.config_manager import config_registry

            # Historically the chat was resizable by default; keep backward
            # compatible behavior by defaulting to True so upgrades don't
            # unexpectedly disable the UX. The variable is still configurable
            # via exposed variables and the config API.
            val = config_registry.get_var(
                "WEBUI_CHAT_RESIZABLE",
                True,
                label="WebUI Chat Resizable",
                description="Allow resizing the chat window in WebUI.",
                component="synth_webui",
                hidden=True,
            )
            return bool(val)
        except Exception:
            return False

    def _dt_to_utc_iso(self, dt: "datetime | None") -> "str | None":
        """Serialize a datetime as an explicit UTC ISO string.

        By default we treat timezone-naive datetimes coming from the DB as UTC
        (attach timezone.utc) to avoid double-shifting when the DB already
        stores UTC timestamps. If you run in an environment where naive
        datetimes are local (e.g. DB session timezone is JST), set the
        environment variable `WEBUI_DB_NAIVE_IS_UTC=0` to revert to the
        legacy behaviour (assume naive == local timezone).
        """
        if not dt:
            return None

        from datetime import timezone
        import os

        # If the datetime is naive, decide whether to treat it as UTC or local
        if dt.tzinfo is None:
            naive_is_utc = os.getenv("WEBUI_DB_NAIVE_IS_UTC", "1").lower() in (
                "1",
                "true",
                "yes",
            )
            if naive_is_utc:
                # Attach UTC tzinfo (do not convert) to preserve the original instant
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                # Legacy behaviour: assume naive datetimes are in the local timezone
                from core.time_zone_utils import get_local_timezone

                dt = dt.replace(tzinfo=get_local_timezone())

        return dt.astimezone(timezone.utc).isoformat()

    async def health(self):
        from datetime import timezone

        return JSONResponse(
            {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
        )

    async def get_action_state_endpoint(self):
        """Get the current global action state."""
        state = await self.action_state_manager.get_current_action()
        if state:
            return JSONResponse(state)
        else:
            return JSONResponse(
                {
                    "action_id": None,
                    "phase": "IDLE",
                    "component": None,
                    "started_at": None,
                }
            )

    async def get_emotion_state_endpoint(self):
        """Get the current emotional state for animation/face expressions.

        Returns JSON with emotion state for dynamic facial expression updates.
        This endpoint is used by the WebUI to fetch emotion data periodically
        for updating the 3D model's facial expressions and animations.

        Example response:
        {
            "emotions": {
                "happy": 7.5,
                "calm": 5.2,
                "curious": 4.0
            },
            "dominant_emotion": "happy"
        }
        """
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            emotion_mgr = None
            try:
                if isinstance(PLUGIN_REGISTRY, dict):
                    emotion_mgr = PLUGIN_REGISTRY.get("emotion_manager")
            except Exception:
                emotion_mgr = None

            emotions = None
            if emotion_mgr and hasattr(emotion_mgr, "get_emotion_state"):
                emotions = await emotion_mgr.get_emotion_state()
            else:
                emotions = {}

            # Find dominant emotion (highest intensity)
            dominant = None
            if emotions:
                dominant = max(emotions.items(), key=lambda x: x[1])[0]

            return JSONResponse(
                {
                    "emotions": emotions,
                    "dominant_emotion": dominant,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

        except Exception as e:
            log_warning(f"{LOG_PREFIX} Failed to get emotion state: {e}")
            # Return neutral state if emotion manager unavailable
            return JSONResponse(
                {
                    "emotions": {},
                    "dominant_emotion": None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "error": str(e),
                }
            )

    async def log_console_endpoint(self, request: Request):
        """Receive console logs from the WebUI frontend and write them to webui.log.

        This endpoint allows the JavaScript console logs (log, error, warn, info)
        to be captured and written to the webui.log file for persistence and debugging.
        """
        try:
            data = await request.json()
            level = data.get("level", "info").upper()
            message = data.get("message", "")

            if message:
                # Log to webui.log with appropriate level
                if level == "ERROR":
                    log_error(f"[console] {message}", log_file=WEBUI_LOG)
                elif level == "WARNING":
                    log_warning(f"[console] {message}", log_file=WEBUI_LOG)
                elif level == "DEBUG":
                    log_debug(f"[console] {message}", log_file=WEBUI_LOG)
                else:  # info
                    log_info(f"[console] {message}", log_file=WEBUI_LOG)

            return JSONResponse({"status": "logged"})
        except Exception as e:
            log_error(f"{LOG_PREFIX} Failed to log console message: {e}")
            return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    async def stats(self):
        uptime = int((datetime.utcnow() - self.start_time).total_seconds())
        return JSONResponse({"uptime": uptime, "sessions": len(self.connections)})

    async def about_summary(self) -> JSONResponse:
        """Return lightweight About tab metadata (uptime, system, and component counts)."""
        uptime = int((datetime.utcnow() - self.start_time).total_seconds())
        sessions = len(self.connections)
        python_version = platform.python_version()
        platform_label = os.getenv("SYNTH_HOST_OS") or platform.platform()
        database_label = os.getenv("SYNTH_PRIMARY_DB") or os.getenv(
            "SYNTH_DB_TYPE", os.getenv("DB_TYPE", "unknown")
        )
        version = os.getenv("SYNTH_VERSION", self.app.version)
        components_count = 0
        try:
            from core.core_initializer import PLUGIN_REGISTRY, INTERFACE_REGISTRY
            from core.cortex_registry import get_cortex_registry

            components_count += len(PLUGIN_REGISTRY)
            components_count += len(INTERFACE_REGISTRY)
            try:
                components_count += len(get_cortex_registry().get_available_engines())
            except Exception:
                pass
        except Exception:
            pass

        return JSONResponse(
            {
                "uptime": uptime,
                "sessions": sessions,
                "components": components_count,
                "messages_today": None,
                "version": version,
                "python": python_version,
                "platform": platform_label,
                "database": database_label,
            }
        )

    async def db_pool_debug(self, request: Request):
        """Return debug information about the DB connection pool.

        This endpoint is intentionally gated by the WEB_DEBUG environment
        variable to avoid exposing internals in production by accident.
        """
        web_debug = os.getenv("WEB_DEBUG", "0").lower()
        if web_debug not in ("1", "true", "yes"):
            raise HTTPException(status_code=403, detail="Debug endpoints disabled")

        try:
            info = core_db.get_pool_debug_info()
            return JSONResponse(info)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to get DB pool debug info: {exc}")
            raise HTTPException(
                status_code=500, detail="Unable to retrieve DB debug info"
            )

    async def debug_inject_message(self, request: Request) -> JSONResponse:
        """Inject a fake LLM response through the full action pipeline.

        Routes the message through ``run_action()`` exactly as if the LLM
        had produced it: facial expression tags are parsed, TTS is triggered
        (when Vox is active), and animation state transitions (THINK → WRITE
        → IDLE) are executed.

        Supports ``[em_*]`` facial expression tags.
        Gated by ``WEB_DEBUG=1``.
        """
        web_debug = os.getenv("WEB_DEBUG", "0").lower()
        if web_debug not in ("1", "true", "yes"):
            raise HTTPException(status_code=403, detail="Debug endpoints disabled")

        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        raw_text: str = body.get("text", "")
        if not raw_text or not raw_text.strip():
            raise HTTPException(status_code=400, detail="'text' is required")

        as_audio: bool = bool(body.get("as_audio", False))

        # Strip [em_*] tags from the text; they drive facial expressions only
        from core.facial_expression_parser import parse_facial_expressions

        clean_text, em_events = parse_facial_expressions(raw_text)

        # Trigger facial expression timeline when tags are present
        if em_events:
            try:
                from plugins.facial_expression_plugin import FacialExpressionPlugin
                from core.animation_handler import get_karada_state_server
                from core.core_initializer import PLUGIN_REGISTRY

                kss = get_karada_state_server()
                has_clients = kss.has_connected_clients() if kss else False
                log_debug(
                    f"{LOG_PREFIX} expression dispatch: kss={kss is not None}, "
                    f"has_clients={has_clients}, em_events={len(em_events)}"
                )
                if has_clients:
                    expr_plugin: Optional[FacialExpressionPlugin] = None
                    if isinstance(PLUGIN_REGISTRY, dict):
                        for p in PLUGIN_REGISTRY.values():
                            if isinstance(p, FacialExpressionPlugin):
                                expr_plugin = p
                                break
                    log_debug(
                        f"{LOG_PREFIX} expression dispatch: expr_plugin={'found' if expr_plugin else 'NOT FOUND'}"
                    )
                    if expr_plugin:
                        from core.persona_manager import get_persona_manager

                        persona_json: Optional[Dict[str, Any]] = None
                        pm = get_persona_manager()
                        if pm and getattr(pm, "_current_persona", None):
                            try:
                                persona_json = pm._load_persona_json(
                                    pm._current_persona.name
                                )
                            except Exception:
                                persona_json = None
                        chars_per_sec = (
                            persona_json.get("facial_expression_chars_per_sec", 12)
                            if persona_json
                            else 12
                        )
                        expr_section = (
                            persona_json.get("facial_expressions", {})
                            if persona_json
                            else {}
                        )
                        log_debug(
                            f"{LOG_PREFIX} expression dispatch: scheduling timeline, "
                            f"events={len(em_events)}, "
                            f"cps={chars_per_sec}, expr_keys={list(expr_section.keys())}"
                        )
                        asyncio.create_task(
                            expr_plugin._play_expression_timeline(
                                em_events,
                                len(clean_text),
                                "",
                                chars_per_sec,
                                expr_section=expr_section,
                            )
                        )
                else:
                    log_debug(
                        f"{LOG_PREFIX} expression dispatch: SKIPPED (no connected clients)"
                    )
            except Exception as exc:
                log_debug(
                    f"{LOG_PREFIX} debug_inject_message expression timeline error: {exc}"
                )

        # Deliver to every connected session through the full action pipeline
        from types import SimpleNamespace
        from core.action_parser import run_actions

        delivered = 0
        errors: List[str] = []
        # Deduplicate on the *logical* session id: several spectator sockets may
        # share one session (Karada v2), but the action pipeline must run once
        # per logical session, not once per physical socket.
        logical_sessions = list(
            dict.fromkeys(
                self._logical_session_id(key) for key in self.connections.keys()
            )
        )
        for session_id in logical_sessions:
            interface_path = f"{INTERFACE_NAME}/{session_id}"

            # Build the action list.  Use clean_text (tags already stripped).
            # When as_audio is set, only send tts_speak — the Vox pipeline
            # calls send_tts_audio → send_message internally, so a separate
            # message_synth_webui would cause a duplicate bubble.
            actions: List[Dict[str, Any]] = []
            if as_audio:
                actions.append(
                    {
                        "type": "tts_speak",
                        "payload": {
                            "text": clean_text,
                            "interface_path": interface_path,
                        },
                    }
                )
            else:
                actions.append(
                    {
                        "type": "message_synth_webui",
                        "payload": {
                            "text": clean_text,
                            "interface_path": interface_path,
                        },
                    }
                )

            context: Dict[str, Any] = {
                "interface_path": interface_path,
                "chat_id": session_id,
                "interface": INTERFACE_NAME,
                "from_cortex": True,
            }

            original_message = SimpleNamespace(
                session_id=session_id,
                interface_path=interface_path,
                from_cortex=True,
                chat_id=session_id,
            )

            try:
                result = await run_actions(actions, context, self, original_message)
                delivered += 1
                if result and result.get("errors"):
                    errors.extend(result["errors"])
            except Exception as exc:
                errors.append(f"{session_id}: {exc}")
                log_debug(
                    f"{LOG_PREFIX} debug_inject_message pipeline error for {session_id}: {exc}"
                )

        log_info(
            f"{LOG_PREFIX} 🧪 Debug inject (pipeline): delivered to {delivered} session(s), "
            f"audio={as_audio}, em_tags={len(em_events)}, text={clean_text[:60]!r}"
        )
        resp: Dict[str, Any] = {"status": "ok", "delivered": delivered}
        if errors:
            resp["warnings"] = errors
        return JSONResponse(resp)

    async def debug_tts_test(self, request: Request) -> JSONResponse:
        """Synthesise arbitrary text through the active Vox (TTS) engine.

        Used by the Debug window to test the currently selected Vox engine /
        model: the text is synthesised and the resulting audio is broadcast to
        the shared avatar (Karada state server) so it can be heard on every
        connected WebUI client. Gated by ``WEB_DEBUG=1``.

        Supports ``[em_*]`` facial expression tags: they are stripped before
        synthesis (so they are never spoken) and drive the avatar's facial
        expression timeline, synchronised to the real audio duration.

        JSON body: ``{"text": "...", "engine": "optional-engine-name"}``.
        """
        web_debug = os.getenv("WEB_DEBUG", "0").lower()
        if web_debug not in ("1", "true", "yes"):
            raise HTTPException(status_code=403, detail="Debug endpoints disabled")

        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        text: str = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="'text' is required")

        engine_name: Optional[str] = body.get("engine") or None

        # Parse [em_*] facial expression tags so the Vox test can drive the
        # avatar's face in sync with the synthesised audio. vox.speak() strips
        # the tags before synthesis, so we parse them here purely to schedule
        # the expression timeline after the audio duration is known.
        from core.facial_expression_parser import parse_facial_expressions

        clean_text, em_events = parse_facial_expressions(text)

        from core.core_initializer import PLUGIN_REGISTRY

        vox = PLUGIN_REGISTRY.get("vox_plugin")
        if vox is None:
            return JSONResponse(
                {
                    "error": (
                        "Vox TTS subsystem is not loaded. Select a Vox engine "
                        "via ACTIVE_VOX_ENGINE."
                    )
                },
                status_code=503,
            )

        try:
            # Generate only — do not dispatch through the normal interface path.
            result = await vox.speak(
                text,
                engine_name=engine_name,
                allow_fallback=False,
                generate_only=True,
            )
            status = result.get("status") if isinstance(result, dict) else None
            if status != "success":
                reason = (
                    result.get("reason", status) if isinstance(result, dict) else status
                )
                return JSONResponse(
                    {"error": f"TTS generation failed: {reason}"},
                    status_code=422,
                )

            audio_path = result.get("audio_path")
            used_engine = engine_name or getattr(vox, "_active_engine_name", None)
            audio_duration_s = (
                result.get("audio_duration_s") if isinstance(result, dict) else None
            )

            delivered = False
            if audio_path:
                delivered = await vox.broadcast_audio_to_webui(
                    audio_path, text=clean_text, engine_name=engine_name
                )

            # Drive the facial expression timeline for any [em_*] tags,
            # synchronised to the real audio duration.
            if em_events and delivered:
                try:
                    interface_path = f"{INTERFACE_NAME}/debug-vox-test"
                    vox._schedule_expression_timeline(
                        em_events, clean_text, interface_path, audio_duration_s
                    )
                except Exception as exc:
                    log_debug(
                        f"{LOG_PREFIX} debug_tts_test expression timeline error: {exc}"
                    )

            log_info(
                f"{LOG_PREFIX} 🔊 Debug Vox test: engine={used_engine}, "
                f"delivered={delivered}, em_tags={len(em_events)}, "
                f"text={clean_text[:60]!r}"
            )
            return JSONResponse(
                {"status": "ok", "engine": used_engine, "delivered": delivered}
            )
        except Exception as exc:
            log_error(f"{LOG_PREFIX} debug_tts_test error: {exc}")
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def debug_expressions(self, request: Request) -> JSONResponse:
        """Return the list of valid facial expressions for the active persona.

        Reads ``facial_expressions`` from the current persona's JSON and
        falls back to a minimal default set.  Gated by ``WEB_DEBUG=1``.
        """
        web_debug = os.getenv("WEB_DEBUG", "0").lower()
        if web_debug not in ("1", "true", "yes"):
            raise HTTPException(status_code=403, detail="Debug endpoints disabled")

        from core.persona_manager import get_persona_manager

        pm = get_persona_manager()

        def _load_expressions(skin_name: str) -> Dict[str, Any]:
            if not pm:
                return {}
            try:
                pj = pm._load_persona_json(skin_name)
            except Exception:
                return {}
            if not pj:
                return {}
            return pj.get("facial_expressions", {}) or {}

        # Prefer the active persona's expressions.  Dynamically loaded skins
        # (e.g. ``temp``) or the ``default`` persona may not declare any, so
        # fall back to the canonical reference skin ``Rei`` — the same skin the
        # animation system uses as its resolution fallback (see AGENTS.md §7) —
        # rather than a partial hardcoded list.
        active_name: Optional[str] = None
        if pm and getattr(pm, "_current_persona", None):
            active_name = getattr(pm._current_persona, "name", None)

        expr_section: Dict[str, Any] = {}
        if active_name:
            expr_section = _load_expressions(active_name)
        if not expr_section:
            expr_section = _load_expressions("Rei")
        if not expr_section:
            expr_section = {
                n: {"description": n}
                for n in ["smile", "grin", "sad", "blush", "surprised", "angry"]
            }

        # Also return canonical emotions from emotion_manager if available
        canonical_emotions: List[str] = []
        try:
            from plugins.emotion_manager import CANONICAL_EMOTIONS

            canonical_emotions = sorted(CANONICAL_EMOTIONS)
        except Exception:
            pass

        return JSONResponse(
            {
                "expressions": {
                    name: info.get("description", name)
                    for name, info in expr_section.items()
                },
                "canonical_emotions": canonical_emotions,
            }
        )

    async def logs_page(self):
        html = self._render_logs()
        return HTMLResponse(content=html)

    async def diary_page(self):
        html = self._render_diary()
        return HTMLResponse(content=html)

    async def serve_template_section(self, section: str):
        """Serve modular template sections for dynamic loading."""
        try:
            import time

            # Validate section name to prevent path traversal
            allowed_sections = {
                "home",
                "skins",
                "logs",
                "diary",
                "history",
                "config",
                "plugins",
                "settings",
                "about",
                "navbar",
                "agent",
                "engines",
            }
            if section not in allowed_sections:
                raise HTTPException(
                    status_code=404, detail="Template section not found"
                )

            # Load template section
            template_path = (
                Path(__file__).parent
                / "webui_templates"
                / "sections"
                / f"{section}.html"
            )
            if not template_path.exists():
                raise HTTPException(
                    status_code=404, detail="Template section not found"
                )

            with open(template_path, "r", encoding="utf-8") as f:
                template = f.read()

            # Apply basic replacements
            replacements = {
                "%%BRAND_NAME%%": BRAND_NAME,
                # cache-busting token so section scripts refresh on each render
                "%%STATIC_VERSION%%": str(int(time.time())),
            }

            for key, value in replacements.items():
                template = template.replace(key, str(value))

            # Inject plugin-registered sub-tabs for this section
            section_tabs = self._plugin_section_tabs.get(section, [])
            for tab in section_tabs:
                # Append button inside <nav class="sub-nav">
                template = template.replace(
                    "</nav>", tab["button_html"] + "\n</nav>", 1
                )
                # Append panel inside .sub-tabs-container (anchored by closing comment)
                template = template.replace(
                    "</div><!-- .sub-tabs-container -->",
                    tab["panel_html"] + "\n      </div><!-- .sub-tabs-container -->",
                    1,
                )

            return HTMLResponse(content=template)

        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Template section not found")
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to serve template section {section}: {exc}")

    async def iframe_host(self, section: str):
        """Serve a lightweight host page that dynamically loads a template section.
        This enables embedding the 'desktop' UI inside an iframe so the top menu
        (header) remains part of the parent document and cannot be hidden by
        content inside the desktop area.
        """
        try:
            # Validate section name to prevent path traversal
            allowed_sections = {
                "home",
                "skins",
                "logs",
                "diary",
                "history",
                "config",
                "components",
                "settings",
                "about",
                "agent",
            }
            if section not in allowed_sections:
                raise HTTPException(
                    status_code=404, detail="Template section not found"
                )

            # Build a small host HTML that will fetch the actual section template and
            # inject it into the iframe's DOM. The host listens to postMessage
            # messages of the form { type: 'load', section: '<name>' } to navigate.
            safe_section = section
            host_html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <base href="/" />
  <title>%%BRAND_NAME%% — embedded</title>
  <style>html,body{height:100%;margin:0;background:transparent;color:var(--text, #fff);font-family:system-ui,Segoe UI,Arial,sans-serif}#root{height:100%;width:100%;box-sizing:border-box}/* Ensure full height */main{height:100%;min-height:0;}</style>
</head>
<body>
  <div id="root"><main id="iframe-root" role="main"></main></div>
  <script>
    (function(){
      async function loadSection(section) {
        try {
          const resp = await fetch('/templates/' + encodeURIComponent(section) + '.html');
          if (!resp.ok) {
            document.getElementById('iframe-root').innerHTML = '<div class="meta">Failed to load section: ' + resp.status + '</div>';
            return;
          }
          const text = await resp.text();
          const parser = new DOMParser();
          const doc = parser.parseFromString(text, 'text/html');
          // Replace root contents
          const root = document.getElementById('iframe-root');
          root.innerHTML = '';
          Array.from(doc.body.children || []).forEach(n => root.appendChild(n));
          // Execute scripts in inserted content (preserve order)
          const scripts = Array.from(root.querySelectorAll('script'));
          for (const old of scripts) {
            const el = document.createElement('script');
            if (old.type) el.type = old.type;
            if (old.src) {
              el.src = old.src;
              el.async = false;
              document.head.appendChild(el);
              await new Promise((resolve) => { el.onload = () => resolve(); el.onerror = () => resolve(); });
            } else {
              el.textContent = old.textContent;
              document.head.appendChild(el);
            }
            old.remove();
          }
        } catch (e) {
          try { document.getElementById('iframe-root').innerText = 'Failed to load section: ' + e; } catch (e) {}
        }
      }
      // Listen for parent messages
      window.addEventListener('message', (ev) => {
        try {
          const d = ev.data || {};
          if (d && d.type === 'load' && d.section) {
            loadSection(d.section);
          }
        } catch (e) { /* ignore */ }
      });
      // Initial load
      loadSection('__SECTION_PLACEHOLDER__');
    })();
  </script>
</body>
</html>"""
            host_html = host_html.replace("__SECTION_PLACEHOLDER__", safe_section)

            # Replace placeholders
            host_html = host_html.replace("%%BRAND_NAME%%", BRAND_NAME)
            return HTMLResponse(content=host_html)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Template section not found")
        except Exception as exc:
            log_error(f"{LOG_PREFIX} iframe_host failed: {exc}")
            raise HTTPException(
                status_code=500, detail=f"Failed to render iframe host: {exc}"
            ) from exc
            raise HTTPException(
                status_code=500, detail="Unable to load template section"
            )

    # ------------------------------------------------------------------
    # WebSocket logic
    # ------------------------------------------------------------------
    async def websocket_endpoint(self, websocket: WebSocket):
        from core.karada_api import _configured_api_token, _token_from_websocket

        expected_token = _configured_api_token()
        if (
            expected_token is not None
            and _token_from_websocket(websocket) != expected_token
        ):
            await websocket.close(code=4401, reason="Invalid or missing API token")
            return

        try:
            client_info = getattr(websocket, "client", None)
            log_debug(f"{LOG_PREFIX} Incoming websocket connection from: {client_info}")
        except Exception:
            pass
        await websocket.accept()
        # Use persistent session id when available (single session per deploy)
        # choose session id depending on mode
        if self._multi_session_enabled():
            session_id = str(uuid.uuid4())
        else:
            # Single-session mode: always use the same fixed ID so that chat
            # history survives container restarts without any file/DB dependency.
            session_id = "webui_default"
            self.session_id = session_id
        # Karada v2 shared-state model: every connected client is a *spectator*
        # of the same unique Synth character. The logical ``session_id`` (chat
        # history / interface_path identity) is intentionally shared in
        # single-session mode, but each physical socket must be registered under
        # a *unique* connection key — otherwise a second WebUI overwrites the
        # first in ``self.connections`` and only the last client receives
        # animation/face/expression broadcasts. In multi-session mode the two
        # values coincide (unique UUID per client).
        if self._multi_session_enabled():
            conn_key = session_id
        else:
            conn_key = f"{session_id}:{uuid.uuid4().hex}"
        self.connections[conn_key] = websocket
        self.message_history.setdefault(session_id, deque(maxlen=self.max_history))
        await websocket.send_json({"type": "session", "session_id": session_id})
        # Ensure persisted history is loaded into memory and replayed
        try:
            await self._ensure_session_history_loaded(session_id)
        except Exception as e:
            log_debug(
                f"{LOG_PREFIX} Failed to load persisted history for {session_id}: {e}"
            )
        # Replay only to *this* freshly connected socket so already-connected
        # spectators of the shared session don't receive duplicate history.
        await self._replay_history(session_id, websocket=websocket)

        # ------------------------------------------------------------------
        # Hello handshake: wait briefly for the client to declare its
        # capabilities and cached assets, then push the full VRM state.
        # ------------------------------------------------------------------
        client_type: str = "unknown"
        client_capabilities: List[str] = ["url_fetch"]
        missing_assets: List[str] = []
        hello_disconnect = False

        try:
            raw_hello = await asyncio.wait_for(websocket.receive_text(), timeout=0.3)
            try:
                hello_payload = json.loads(raw_hello)
            except json.JSONDecodeError:
                hello_payload = {}

            if hello_payload.get("type") == "hello":
                client_type = hello_payload.get("client_type", "unknown")
                client_capabilities = hello_payload.get("capabilities", ["url_fetch"])
                has_assets = hello_payload.get("has_assets", [])
                if self.animation_handler and has_assets:
                    missing_assets = self.animation_handler.get_missing_assets(
                        has_assets
                    )
                log_info(
                    f"{LOG_PREFIX} Hello from {session_id}: type={client_type}, "
                    f"caps={client_capabilities}, missing_assets={len(missing_assets)}"
                )
            elif hello_payload.get("text"):
                # Client sent a chat message instead of hello — process normally
                text = (hello_payload.get("text") or "").strip()
                if text:
                    await self._append_history(session_id, "user", text)
                    asyncio.create_task(self._handle_user_message(session_id, text))
        except asyncio.TimeoutError:
            log_debug(
                f"{LOG_PREFIX} No hello from {session_id} within timeout, proceeding"
            )
        except WebSocketDisconnect:
            # Client dropped during the handshake (page reload / reconnect
            # churn). Skip the state push and the receive loop — attempting
            # either on a closed socket logs a spurious warning ("Failed to
            # push VRM state ... after 'websocket.close'") followed by an
            # error ("Cannot call 'receive' once a disconnect message has
            # been received").
            hello_disconnect = True
        except Exception as hello_exc:
            log_debug(f"{LOG_PREFIX} Hello handling error (non-fatal): {hello_exc}")

        # The disconnect can also be consumed without raising (e.g. the
        # ``wait_for`` cancellation races the close frame), so double-check
        # the socket state rather than trusting the exception alone.
        if (
            websocket.client_state is not WebSocketState.CONNECTED
            or websocket.application_state is not WebSocketState.CONNECTED
        ):
            hello_disconnect = True

        # Push full VRM state to the newly connected client
        try:
            if self.animation_handler and not hello_disconnect:
                full_state = await self.animation_handler.get_full_state()

                # 1) VRM model
                vrm = full_state.get("vrm_model", {})
                if vrm.get("url"):
                    await websocket.send_json(
                        {
                            "type": "vrm_model",
                            "name": vrm.get("name"),
                            "url": vrm.get("url"),
                            "hash": vrm.get("hash"),
                        }
                    )

                # 2) Current animation (if any)
                anim = full_state.get("animation", {})
                if anim.get("descriptor"):
                    await websocket.send_json(
                        {
                            "type": "vrm_animation_v2",
                            "state": anim.get("state", "idle"),
                            "descriptor": anim.get("descriptor"),
                            "started_at": anim.get("started_at"),
                            "restore": True,
                        }
                    )

                # 3) Preload idle variants in background (non-blocking)
                asyncio.create_task(self.animation_handler.ensure_idle_preloaded())

                # 4) Face values (emotions)
                face = full_state.get("face_values", {})
                if face:
                    await websocket.send_json({"type": "vrm_face", "values": face})

                # 5) If no animation is set yet, start idle
                if not anim.get("descriptor") and self.persona_manager:
                    await self.persona_manager.set_animation_state("idle")
                    log_debug(f"{LOG_PREFIX} Started idle animation for first session")
        except Exception as push_exc:
            log_warning(
                f"{LOG_PREFIX} Failed to push VRM state to session {session_id}: {push_exc}"
            )

        if not hello_disconnect:
            log_info(
                f"{LOG_PREFIX} Client connected: {session_id} (type={client_type})"
            )

        try:
            if hello_disconnect:
                # Route through the normal disconnect path so the shared
                # cleanup in ``finally`` runs exactly once.
                raise WebSocketDisconnect(code=1000)
            while True:
                data = await websocket.receive_text()
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    payload = {"text": data}

                # Ignore control messages that are not chat text
                msg_type = payload.get("type")
                if msg_type in ("hello",):
                    continue
                if msg_type == "touch":
                    try:
                        await self._handle_touch_animation_request(session_id, payload)
                    except Exception as touch_exc:
                        log_warning(
                            f"{LOG_PREFIX} Failed to handle touch interaction from {session_id}: {touch_exc}"
                        )
                    continue
                if msg_type == "interaction":
                    try:
                        await self._handle_interaction_event(session_id, payload)
                    except Exception as inter_exc:
                        log_warning(
                            f"{LOG_PREFIX} Failed to handle interaction event from {session_id}: {inter_exc}"
                        )
                    continue

                text = (payload.get("text") or "").strip()
                attachments = payload.get("attachments") or []
                if not text and not attachments:
                    continue
                is_voice_input = bool(payload.get("is_voice_input", False))
                normalized_attachments = [
                    self._normalize_webui_attachment(att) for att in attachments
                ]
                metadata = (
                    {"attachments": normalized_attachments}
                    if normalized_attachments
                    else None
                )
                await self._append_history(session_id, "user", text, metadata=metadata)
                # Process message in background to avoid blocking WebSocket
                asyncio.create_task(
                    self._handle_user_message(
                        session_id,
                        text,
                        attachments=normalized_attachments,
                        is_voice_input=is_voice_input,
                    )
                )
        except WebSocketDisconnect:
            log_info(
                f"{LOG_PREFIX} Client disconnected: {session_id} (conn={conn_key})"
            )
        except Exception as exc:  # pragma: no cover - runtime issues
            log_error(f"{LOG_PREFIX} websocket error: {exc}")
        finally:
            # Remove only this physical socket. In single-session mode the
            # logical history is shared across all spectator clients, so only
            # drop it once the last connection for this session_id is gone.
            self.connections.pop(conn_key, None)
            still_connected = any(
                key == session_id or key.startswith(f"{session_id}:")
                for key in self.connections
            )
            if not still_connected:
                self.message_history.pop(session_id, None)

    async def logs_ws_endpoint(
        self, websocket: WebSocket
    ):  # pragma: no cover - runtime streaming
        await websocket.accept()
        log_info(f"{LOG_PREFIX} Log stream WebSocket connected")

        log_override = (self.log_source_path or "").strip()
        candidates = []
        if log_override:
            candidates.append(Path(log_override).expanduser())
        candidates.extend(
            [
                Path("/app/logs/synth.log"),
                Path.cwd() / "logs" / "synth.log",
                Path.cwd() / "logs" / "dev" / "synth.log",
                Path(_LOG_FILE),
            ]
        )

        unique_candidates = []
        seen = set()
        for candidate in candidates:
            if candidate is None:
                continue
            candidate = Path(candidate)
            key = candidate.expanduser()
            if key in seen:
                continue
            seen.add(key)
            unique_candidates.append(candidate)

        log_debug(
            f"{LOG_PREFIX} Log file candidates: {[str(c) for c in unique_candidates]}"
        )
        path = next(
            (candidate for candidate in unique_candidates if candidate.exists()),
            unique_candidates[0],
        )
        log_debug(f"{LOG_PREFIX} Selected log file: {path} (exists: {path.exists()})")

        try:
            # Prepare list of exception types that are considered 'normal' disconnects
            try:
                from websockets.exceptions import (
                    ConnectionClosedOK,
                    ConnectionClosedError,
                )
            except Exception:
                ConnectionClosedOK = ConnectionClosedError = None  # type: ignore
            try:
                from uvicorn.protocols.utils import ClientDisconnected
            except Exception:
                ClientDisconnected = None  # type: ignore
            from starlette.websockets import WebSocketDisconnect

            disconnect_exceptions = tuple(
                [
                    exc
                    for exc in (
                        ConnectionClosedOK,
                        ConnectionClosedError,
                        ClientDisconnected,
                        WebSocketDisconnect,
                    )
                    if exc
                ]
            )

            wait_seconds = self.log_wait_seconds if self.log_wait_seconds else 20
            waited = 0
            while not path.exists() and waited < wait_seconds:
                log_debug(
                    f"{LOG_PREFIX} Waiting for log file... ({waited}/{wait_seconds}s)"
                )
                await asyncio.sleep(1)
                waited += 1

            if not path.exists():
                error_msg = f"Log file not found: {path}"
                log_warning(f"{LOG_PREFIX} {error_msg}")
                try:
                    await websocket.send_text(error_msg)
                except Exception:
                    # Client probably disconnected before we could send
                    log_debug(
                        f"{LOG_PREFIX} Client disconnected before receiving 'log not found' message"
                    )
                return

            log_debug(f"{LOG_PREFIX} Opening log file: {path}")
            with path.open("r", encoding="utf-8", errors="replace") as log_file:
                # Send last 200 lines
                log_file.seek(0)
                recent_lines = deque(log_file, maxlen=200)
                for line in recent_lines:
                    try:
                        await websocket.send_text(line.rstrip())
                    except Exception as exc:
                        # If the client disconnected, stop streaming silently
                        if isinstance(exc, disconnect_exceptions) or isinstance(
                            exc, (BrokenPipeError, ConnectionResetError)
                        ):
                            log_info(
                                f"{LOG_PREFIX} Log stream websocket disconnected while sending history: {type(exc).__name__}"
                            )
                            return
                        # Otherwise log and break
                        log_error(f"{LOG_PREFIX} Failed to send log line: {exc}")
                        return
                log_file.seek(0, os.SEEK_END)
                while True:
                    line = log_file.readline()
                    if not line:
                        await asyncio.sleep(1)
                        continue
                    try:
                        await websocket.send_text(line.rstrip())
                    except Exception as exc:
                        if isinstance(exc, disconnect_exceptions) or isinstance(
                            exc, (BrokenPipeError, ConnectionResetError)
                        ):
                            log_info(
                                f"{LOG_PREFIX} Log stream websocket disconnected while streaming: {type(exc).__name__}"
                            )
                            return
                        import traceback

                        log_error(f"{LOG_PREFIX} log stream error: {exc}")
                        log_error(f"{LOG_PREFIX} Exception type: {type(exc).__name__}")
                        log_error(f"{LOG_PREFIX} Traceback: {traceback.format_exc()}")
                        try:
                            await websocket.send_text(
                                f"--- log stream error: {exc} ---"
                            )
                        except Exception:
                            pass  # Websocket might be closed already
        finally:
            try:
                await websocket.close()
            except Exception:
                pass  # Websocket might already be closed

    # ------------------------------------------------------------------
    # Auris audio endpoints
    # ------------------------------------------------------------------

    async def audio_upload_endpoint(
        self,
        file: UploadFile = File(...),
        engine: Optional[str] = Form(None),
    ):
        """POST /api/audio/upload — transcribe an uploaded audio file via Auris.

        Multipart form fields:
        - ``file``: the audio file (wav, ogg, mp4, …)
        - ``engine``: optional engine override (default: active auris engine)

        Returns JSON: ``{"text": "...", "engine": "..."}`` or
        ``{"error": "..."}`` with an appropriate HTTP status.
        """
        if not file or not file.filename:
            raise HTTPException(status_code=400, detail="No file uploaded")

        try:
            from core.core_initializer import PLUGIN_REGISTRY

            import tempfile

            auris = PLUGIN_REGISTRY.get("auris_plugin")
            if auris is None:
                raise HTTPException(
                    status_code=503,
                    detail="Auris STT subsystem is not loaded. Select an Auris engine or disable via ACTIVE_AURIS_ENGINE.",
                )
            tmp_dir = Path(tempfile.mkdtemp())
            tmp_dir.mkdir(parents=True, exist_ok=True)
            suffix = Path(file.filename).suffix or ".audio"
            tmp_path = tmp_dir / f"webui_{uuid.uuid4().hex}{suffix}"
            try:
                contents = await file.read()
                tmp_path.write_bytes(contents)

                mime_hint = file.content_type or None
                transcribed_engine = engine or getattr(
                    auris, "_active_engine_name", None
                )
                _auris_result = await auris.transcribe_audio(
                    str(tmp_path), mime_type=mime_hint, engine_name=engine
                )
                text = _auris_result.text if _auris_result else None
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

            if text is None:
                return JSONResponse(
                    {"error": "Transcription returned no text"},
                    status_code=422,
                )
            return JSONResponse({"text": text, "engine": transcribed_engine})

        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} audio_upload_endpoint error: {exc}")
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ------------------------------------------------------------------
    # WebUI chat attachment upload endpoint
    # ------------------------------------------------------------------

    async def chat_attachment_upload_endpoint(
        self,
        file: UploadFile = File(...),
    ):
        """POST /api/chat/attachments — store a user attachment for WebUI chat."""
        if not file or not file.filename:
            raise HTTPException(status_code=400, detail="No file uploaded")

        filename = Path(file.filename).name
        safe_name = f"{uuid.uuid4().hex}_{filename}"
        destination = self.attachments_dir / safe_name
        try:
            with destination.open("wb") as fh:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to store chat attachment: {exc}")
            raise HTTPException(status_code=500, detail="Failed to store uploaded file")

        file_url = f"/uploads/{quote(safe_name)}"
        return JSONResponse(
            {
                "status": "ok",
                "url": file_url,
                "filename": filename,
                "mime_type": file.content_type or "application/octet-stream",
                "size": destination.stat().st_size,
            }
        )

    # ------------------------------------------------------------------
    # Vox metadata/sample endpoints
    # ------------------------------------------------------------------

    async def vox_speakers(self, request: Request):
        """GET /api/vox/speakers?engine=<name>"""
        from core.config_manager import config_registry
        from core.vox_registry import VOX_REGISTRY

        engine_name = request.query_params.get("engine") or config_registry.get_value(
            "ACTIVE_VOX_ENGINE", "kitten", value_type=str
        )
        reg = VOX_REGISTRY
        try:
            engine = reg.load_engine(engine_name)
        except ValueError:
            raise HTTPException(status_code=404, detail="Engine not found")
        try:
            speakers = engine.get_speakers()
        except Exception:
            speakers = []
        return JSONResponse(speakers)

    async def vox_sample(self, request: Request):
        """GET /api/vox/sample?engine=<name>&speaker=<code>"""
        from core.config_manager import config_registry
        from core.vox_registry import VOX_REGISTRY

        engine_name = request.query_params.get("engine") or config_registry.get_value(
            "ACTIVE_VOX_ENGINE", "kitten", value_type=str
        )
        speaker = request.query_params.get("speaker")
        if not speaker:
            raise HTTPException(status_code=400, detail="speaker parameter required")
        reg = VOX_REGISTRY
        try:
            engine = reg.load_engine(engine_name)
        except ValueError:
            raise HTTPException(status_code=404, detail="Engine not found")
        try:
            data = engine.sample(speaker)
        except NotImplementedError:
            raise HTTPException(status_code=404, detail="No sample available")
        return Response(data, media_type="audio/wav")

    # ------------------------------------------------------------------
    # Model management endpoints  (SSOT: core.model_manager.MODEL_MANAGER)
    # ------------------------------------------------------------------

    async def vosk_model_download(self, language: str = Form(...)):
        """POST /api/auris/vosk/download  — legacy compat shim.

        Looks up the Vosk model_id for *language* and delegates to
        ``MODEL_MANAGER.download()``.  Also updates ``VOSK_LANGUAGE`` so the
        engine picks up the new model on next load.
        """
        try:
            from core.config_manager import config_registry
            from core.model_manager import MODEL_MANAGER

            # lang → model_id mapping (mirrors vosk_engine._LANG_TO_MODEL_ID)
            _LANG_MAP: dict[str, str] = {
                "en": "vosk-en-us",
                "en-us": "vosk-en-us",
                "en-gb": "vosk-en-us",
                "it": "vosk-it-it",
                "it-it": "vosk-it-it",
                "fr": "vosk-fr-fr",
                "fr-fr": "vosk-fr-fr",
                "es": "vosk-es-es",
                "es-es": "vosk-es-es",
                "de": "vosk-de-de",
                "de-de": "vosk-de-de",
                "pt": "vosk-pt-pt",
                "pt-pt": "vosk-pt-pt",
                "zh": "vosk-zh-cn",
                "zh-cn": "vosk-zh-cn",
                "ja": "vosk-ja-jp",
                "ja-jp": "vosk-ja-jp",
                "ko": "vosk-ko-kr",
                "ko-kr": "vosk-ko-kr",
            }
            model_id = _LANG_MAP.get(language.lower(), f"vosk-{language.lower()}")
            config_registry.set_value("VOSK_LANGUAGE", language)
            # Fire-and-forget in background so the endpoint always returns immediately;
            # clients can poll /api/models/{id}/progress to track completion.
            import asyncio as _asyncio

            _asyncio.create_task(MODEL_MANAGER.download(model_id))
            log_info(f"{LOG_PREFIX} vosk_model_download: started {model_id}")
            return JSONResponse(
                {"success": True, "language": language, "model_id": model_id}
            )
        except Exception as exc:
            log_error(f"{LOG_PREFIX} vosk_model_download error: {exc}")
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def list_models(self, request: Request) -> JSONResponse:
        """GET /api/models[?plugin_id=<id>]  — return all registered models with status."""
        from core.model_manager import MODEL_MANAGER

        plugin_filter = request.query_params.get("plugin_id")
        catalog = MODEL_MANAGER.catalog()
        if plugin_filter:
            catalog = [m for m in catalog if m["plugin_id"] == plugin_filter]
        return JSONResponse({"models": catalog})

    async def get_model_detail(self, model_id: str) -> JSONResponse:
        """GET /api/models/{model_id}  — return single model info with sample list."""
        from core.model_manager import MODEL_MANAGER

        spec = MODEL_MANAGER.get_spec(model_id)
        if not spec:
            raise HTTPException(
                status_code=404, detail=f"Model '{model_id}' not registered"
            )
        catalog_entry = next(
            (m for m in MODEL_MANAGER.catalog() if m["model_id"] == model_id), None
        )
        if not catalog_entry:
            raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
        catalog_entry["samples"] = MODEL_MANAGER.list_samples(model_id)
        return JSONResponse(catalog_entry)

    async def start_model_download(self, model_id: str) -> JSONResponse:
        """POST /api/models/{model_id}/download  — start async download of a model."""
        from core.model_manager import MODEL_MANAGER

        spec = MODEL_MANAGER.get_spec(model_id)
        if not spec:
            raise HTTPException(
                status_code=404, detail=f"Model '{model_id}' not registered"
            )
        if MODEL_MANAGER.is_downloaded(model_id):
            return JSONResponse({"status": "already_downloaded", "model_id": model_id})
        if MODEL_MANAGER.download_progress(model_id) is not None:
            return JSONResponse({"status": "in_progress", "model_id": model_id})
        # Fire-and-forget in background
        import asyncio as _asyncio

        _asyncio.create_task(MODEL_MANAGER.download(model_id))
        log_info(f"{LOG_PREFIX} Model download started: {model_id}")
        return JSONResponse({"status": "started", "model_id": model_id})

    async def get_model_progress(self, model_id: str) -> JSONResponse:
        """GET /api/models/{model_id}/progress  — poll download progress (0-1) or status."""
        from core.model_manager import MODEL_MANAGER

        progress = MODEL_MANAGER.download_progress(model_id)
        downloaded = MODEL_MANAGER.is_downloaded(model_id)
        return JSONResponse(
            {
                "model_id": model_id,
                "downloaded": downloaded,
                "in_progress": progress is not None,
                "progress": progress,
            }
        )

    async def delete_model(self, model_id: str) -> JSONResponse:
        """DELETE /api/models/{model_id}  — remove a downloaded model from disk."""
        from core.model_manager import MODEL_MANAGER

        spec = MODEL_MANAGER.get_spec(model_id)
        if not spec:
            raise HTTPException(
                status_code=404, detail=f"Model '{model_id}' not registered"
            )
        if not MODEL_MANAGER.is_downloaded(model_id):
            raise HTTPException(
                status_code=404, detail=f"Model '{model_id}' is not downloaded"
            )
        ok = MODEL_MANAGER.delete(model_id)
        if ok:
            return JSONResponse({"deleted": True, "model_id": model_id})
        return JSONResponse(
            {"deleted": False, "error": "Delete failed"}, status_code=500
        )

    async def model_sample_voice(
        self, model_id: str, voice: str, request: Request
    ) -> Response:
        """GET /api/models/{model_id}/sample/{voice}?lang=en  — stream a sample MP3.

        Returns 404 if the sample does not exist for the requested language so
        the client can show a "Generate" button instead of a play button.
        """
        from core.model_manager import MODEL_MANAGER

        lang = request.query_params.get("lang", "en")
        # Try pre-generated file first
        samples = MODEL_MANAGER.list_samples(model_id, lang=lang)
        sample = next((s for s in samples if s.get("voice") == voice), None)
        if sample:
            path = Path(sample["path"])
            if path.exists():
                return Response(path.read_bytes(), media_type="audio/mpeg")
        # No pre-generated file: 404 so the UI knows to show Generate button
        raise HTTPException(
            status_code=404,
            detail=f"Sample for '{model_id}/{voice}' (lang={lang}) not yet generated",
        )

    async def model_voice_sample_exists(
        self, model_id: str, voice: str, request: Request
    ) -> JSONResponse:
        """GET /api/models/{model_id}/voice/{voice}/exists?lang=en

        Lightweight existence check — returns ``{"exists": true/false, "url": str|null}``.
        """
        from core.model_manager import MODEL_MANAGER

        lang = request.query_params.get("lang", "en")
        exists = MODEL_MANAGER.sample_exists(model_id, voice, lang)
        url: str | None = None
        if exists:
            url = MODEL_MANAGER._sample_url(model_id, voice, lang)
        return JSONResponse(
            {
                "exists": exists,
                "url": url,
                "model_id": model_id,
                "voice": voice,
                "lang": lang,
            }
        )

    async def model_generate_voice_sample(
        self, model_id: str, voice: str, request: Request
    ) -> JSONResponse:
        """POST /api/models/{model_id}/voice/{voice}/generate?lang=en

        Generate a voice sample on-the-go.  Runs synchronously in an executor
        so the endpoint returns the result (or an error) once generation is
        complete (usually < 15 s).

        Returns ``{"url": "..."}`` on success or ``{"error": "..."}`` with HTTP 500.
        """
        import asyncio as _asyncio
        from core.model_manager import MODEL_MANAGER
        from core.vox_registry import VOX_REGISTRY

        lang = request.query_params.get("lang", "en")

        spec = MODEL_MANAGER.get_spec(model_id)
        if not spec:
            raise HTTPException(
                status_code=404, detail=f"Model '{model_id}' not registered"
            )
        if not MODEL_MANAGER.is_downloaded(model_id):
            raise HTTPException(
                status_code=404,
                detail=f"Model '{model_id}' is not downloaded",
            )
        if voice not in spec.voices:
            raise HTTPException(
                status_code=404,
                detail=f"Voice '{voice}' not found in model '{model_id}'",
            )

        # Resolve gender for this voice (needed by edge-tts gender filter)
        gender = "N"
        for vm in spec.voices_meta:
            if vm.name == voice:
                gender = vm.gender
                break

        # Import generator helper lazily to avoid circular imports
        import inspect

        def _generate_fn(text: str, v: str | None) -> "bytes | None":
            # Try VOX engine first
            for engine_name in VOX_REGISTRY.get_available_engines():
                try:
                    engine = VOX_REGISTRY.load_engine(engine_name)
                    if not hasattr(engine, "sample"):
                        continue
                    sig = inspect.signature(engine.sample)
                    data: bytes | None = (
                        engine.sample(text, v)
                        if len(sig.parameters) == 2
                        else engine.sample(v)
                    )
                    if data:
                        return data
                except Exception:
                    continue
            # edge-tts fallback with correct locale + gender
            try:
                from scripts.generate_model_samples import _edge_generate, _tweak_audio

                data = _edge_generate(text, v, lang=lang, gender=gender)
                if data:
                    return _tweak_audio(data, v, lang)
            except Exception:
                pass
            return None

        loop = _asyncio.get_event_loop()
        try:
            path = await loop.run_in_executor(
                None,
                lambda: MODEL_MANAGER.ensure_sample(
                    model_id, voice, _generate_fn, lang=lang
                ),
            )
        except Exception as exc:
            log_error(f"{LOG_PREFIX} model_generate_voice_sample error: {exc}")
            return JSONResponse({"error": str(exc)}, status_code=500)

        if not path:
            return JSONResponse({"error": "Sample generation failed"}, status_code=500)

        url = MODEL_MANAGER._sample_url(model_id, voice, lang)
        return JSONResponse(
            {"url": url, "model_id": model_id, "voice": voice, "lang": lang}
        )

    async def audio_stream_ws_endpoint(
        self, websocket: WebSocket
    ):  # pragma: no cover - runtime streaming
        """WebSocket /api/audio/stream — real-time STT via VAD or a Live engine.

        Protocol (client → server):
        - Text frame ``{"sample_rate": 16000, "engine": "vad"}``: **must** be
          sent first to configure the session.
          ``engine`` values: ``"vad"`` / ``"silero"`` → use the core
          ``VADService`` (local, always available when silero-vad is installed);
          any other value → route to the Live streaming registry.
        - Binary frames: raw PCM s16le audio chunks at the negotiated sample rate.

        Protocol (server → client):
        - ``{"type": "ready",   "session_id": "..."}`` — sent once after setup.
        - ``{"type": "partial", "text": "..."}`` — interim transcript segment.
        - ``{"type": "final",   "text": "..."}`` — final transcript (is_final=True).
        - ``{"type": "vad",     "signal": "speech_start"|"speech_end"}`` — VAD events.
        - ``{"type": "error",   "detail": "..."}`` — error notification.
        """
        from core.karada_api import _configured_api_token, _token_from_websocket

        expected_token = _configured_api_token()
        if (
            expected_token is not None
            and _token_from_websocket(websocket) != expected_token
        ):
            await websocket.close(code=4401, reason="Invalid or missing API token")
            return

        await websocket.accept()
        session_id = f"ws_{uuid.uuid4().hex}"
        live_engine = None  # only set when using LIVE_REGISTRY

        try:
            import json as _json

            from core.vad_service import VAD_SERVICE

            # ── Config frame ────────────────────────────────────────────────
            sample_rate = 16000
            engine_name = "vad"
            try:
                config_raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=5.0
                )
                config = _json.loads(config_raw)
                sample_rate = int(config.get("sample_rate", 16000))
                engine_name = str(config.get("engine", engine_name))
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

            # ── Route: core VADService vs Live registry ──────────────────────
            _VAD_ALIASES = {"vad", "silero", ""}

            if engine_name in _VAD_ALIASES:
                # ── VAD path (core service) ──────────────────────────────────
                if not VAD_SERVICE.is_available():
                    await websocket.send_json(
                        {
                            "type": "error",
                            "detail": (
                                "Silero VAD not available. "
                                "Install the 'silero' extra: "
                                "pip install '.[silero]'"
                            ),
                        }
                    )
                    await websocket.close()
                    return

                VAD_SERVICE.open_session(session_id)
                await websocket.send_json({"type": "ready", "session_id": session_id})
                log_info(
                    f"{LOG_PREFIX} VAD stream session started: {session_id}, sr={sample_rate}"
                )

                try:
                    while True:
                        try:
                            data = await asyncio.wait_for(
                                websocket.receive_bytes(), timeout=30.0
                            )
                        except asyncio.TimeoutError:
                            break
                        except WebSocketDisconnect:
                            break
                        except Exception:
                            break

                        events = VAD_SERVICE.process_chunk(
                            session_id, data, sample_rate
                        )
                        for evt in events:
                            try:
                                await websocket.send_json(
                                    {"type": "vad", "signal": evt}
                                )
                            except Exception:
                                break
                finally:
                    pending = VAD_SERVICE.close_session(session_id)
                    for evt in pending:
                        try:
                            await websocket.send_json({"type": "vad", "signal": evt})
                        except Exception:
                            pass

            else:
                # ── Live engine path (bidirectional, e.g. gemini_live) ────────
                from core.live_registry import LIVE_REGISTRY
                from plugins.live_base import LiveEventType

                try:
                    live_engine = LIVE_REGISTRY.load_engine(engine_name)
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
                    await websocket.close()
                    return

                if not live_engine.supports_input:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "detail": (
                                f"Live engine '{engine_name}' does not support audio input / STT. "
                                "Use POST /api/audio/upload for file-based transcription instead."
                            ),
                        }
                    )
                    await websocket.close()
                    return

                await live_engine.open_session(session_id, sample_rate=sample_rate)
                await websocket.send_json({"type": "ready", "session_id": session_id})

                log_info(
                    f"{LOG_PREFIX} Live stream session started: {session_id}, "
                    f"engine={engine_name}, sr={sample_rate}"
                )

                async def _receive_audio() -> None:
                    while True:
                        try:
                            data = await asyncio.wait_for(
                                websocket.receive_bytes(), timeout=30.0
                            )
                            await live_engine.send_audio(session_id, data, sample_rate)
                        except asyncio.TimeoutError:
                            break
                        except WebSocketDisconnect:
                            break
                        except Exception:
                            break

                async def _forward_events() -> None:
                    async for event in live_engine.receive_events(session_id):
                        try:
                            if event.type == LiveEventType.TRANSCRIPT:
                                etype = "final" if event.is_final else "partial"
                                await websocket.send_json(
                                    {"type": etype, "text": event.text}
                                )
                            elif event.type == LiveEventType.VAD:
                                await websocket.send_json(
                                    {"type": "vad", "signal": event.vad_signal}
                                )
                            elif event.type == LiveEventType.AUDIO and event.audio:
                                await websocket.send_bytes(event.audio)
                            elif event.type == LiveEventType.ERROR:
                                await websocket.send_json(
                                    {"type": "error", "detail": event.detail}
                                )
                        except Exception:
                            break

                await _receive_audio()
                await live_engine.close_session(session_id)
                live_engine = None
                await _forward_events()

        except Exception as exc:
            log_error(f"{LOG_PREFIX} audio_stream_ws_endpoint error: {exc}")
            try:
                await websocket.send_json({"type": "error", "detail": str(exc)})
            except Exception:
                pass
        finally:
            if live_engine is not None:
                try:
                    await live_engine.close_session(session_id)
                except Exception:
                    pass
            try:
                await websocket.close()
            except Exception:
                pass

    async def _broadcast_action_state(self, state: Optional[Dict[str, Any]]) -> None:
        """
        Broadcast the current action state to all connected WebSocket clients.

        Called whenever the global action state changes.
        """
        if not state:
            # Stack is empty, return to IDLE
            message = {
                "type": "action_state",
                "phase": "IDLE",
                "action_id": None,
                "component": None,
            }
        else:
            message = {
                "type": "action_state",
                "phase": state.get("phase"),
                "action_id": state.get("action_id"),
                "component": state.get("component"),
            }

        log_info(
            f"{LOG_PREFIX} Broadcasting action state to {len(self.connections)} clients: {message['phase']}"
        )

        # Backend-authoritative animation playback:
        # whenever the global action phase changes, trigger the KaradaStateServer to
        # play the corresponding logical state. Plugins can override per-state
        # animations via KaradaStateServer registration, but the backend remains the
        # source of truth for *when* a state starts/ends.
        try:
            phase = message.get("phase") or AnimationPhase.IDLE.value
            phase_to_state = {
                AnimationPhase.THINKING.value: AnimationState.THINK,
                AnimationPhase.WRITING.value: AnimationState.WRITE,
                AnimationPhase.CORRECTING.value: AnimationState.THINK,
                AnimationPhase.TALKING.value: AnimationState.TALK,
                AnimationPhase.IDLE.value: AnimationState.IDLE,
            }
            anim_state = phase_to_state.get(str(phase), AnimationState.IDLE)
            if self.animation_handler:
                # session_id=None broadcasts to all connected WebUI sessions.
                await self.animation_handler.play_animation(
                    anim_state, session_id=None, loop=True
                )
        except Exception as exc:
            log_warning(
                f"{LOG_PREFIX} Failed to trigger animation for action phase change: {exc}"
            )

        # Send to all connected clients
        for session_id, websocket in self.connections.items():
            try:
                await websocket.send_json(message)
                log_info(
                    f"{LOG_PREFIX} ✓ Sent action_state to session {session_id}: {message['phase']}"
                )
            except Exception as exc:
                log_warning(
                    f"{LOG_PREFIX} Failed to broadcast action state to session {session_id}: {exc}"
                )

    async def broadcast_event(self, event: str, payload: Dict[str, Any]) -> None:
        """Broadcast a custom event to all connected WebUI clients."""
        message = {"type": event, **(payload or {})}
        log_debug(
            f"{LOG_PREFIX} Broadcasting event '{event}' to {len(self.connections)} clients"
        )
        for session_id, websocket in list(self.connections.items()):
            try:
                await websocket.send_json(message)
            except Exception as exc:
                log_warning(
                    f"{LOG_PREFIX} Failed to broadcast event '{event}' to {session_id}: {exc}"
                )

    async def _broadcast_animation_state(
        self,
        state: AnimationState,
        animation_file: str,
        descriptor: Optional[Dict[str, Any]],
    ) -> None:
        """No-op stub kept for external plugin compatibility.

        Broadcasting is now done directly by ``KaradaStateServer`` via
        ``_send_animation_command`` which always iterates over all connections.
        Plugins that registered this as a callback will call it harmlessly.
        """

    async def _broadcast_animation_state_summary(
        self,
        state: AnimationState,
        animation_file: str,
        descriptor: Optional[Dict[str, Any]],
    ) -> None:
        """Broadcast a lightweight animation-state summary to connected clients."""
        if not self.connections:
            return

        current: Dict[str, Any] = {}
        if self.animation_handler:
            try:
                current = self.animation_handler.get_current_animation_state() or {}
            except Exception:
                current = {}

        animation_state = {
            "descriptor": current.get("descriptor"),
            "started_at": current.get("started_at"),
            "expressions": descriptor.get("expressions")
            if isinstance(descriptor, dict)
            else None,
            "blink": descriptor.get("blink") if isinstance(descriptor, dict) else None,
            "eye_movement": (
                descriptor.get("eye_movement") if isinstance(descriptor, dict) else None
            ),
            "emotions": None,
            "lipsync": (
                descriptor.get("lipsync")
                if isinstance(descriptor, dict) and "lipsync" in descriptor
                else False
            ),
        }

        emotions = None
        try:
            emotion_mgr = None
            emotion_manager_cls = None
            try:
                from plugins.emotion_manager import EmotionManager

                emotion_manager_cls = EmotionManager
            except Exception:
                emotion_manager_cls = None

            try:
                from core.core_initializer import PLUGIN_REGISTRY

                if isinstance(PLUGIN_REGISTRY, dict):
                    candidate = PLUGIN_REGISTRY.get("emotion_manager")
                    if candidate is not None and hasattr(
                        candidate, "get_emotion_state"
                    ):
                        if emotion_manager_cls is None or isinstance(
                            candidate, emotion_manager_cls
                        ):
                            emotion_mgr = candidate
            except Exception:
                emotion_mgr = None

            if emotion_mgr is None and emotion_manager_cls is not None:
                try:
                    emotion_mgr = emotion_manager_cls()
                except Exception:
                    emotion_mgr = None

            emotions_raw = None
            if emotion_mgr is not None and hasattr(emotion_mgr, "get_emotion_state"):
                emotions_raw_maybe = emotion_mgr.get_emotion_state()
                emotions_raw = (
                    await emotions_raw_maybe
                    if asyncio.iscoroutine(emotions_raw_maybe)
                    else emotions_raw_maybe
                )

            if isinstance(emotions_raw, dict) and emotions_raw:
                emotions_filtered = {
                    key: value
                    for key, value in emotions_raw.items()
                    if isinstance(value, (int, float)) and value >= 0.1
                }
                if emotions_filtered:
                    dominant, _ = max(
                        emotions_filtered.items(), key=lambda item: item[1]
                    )
                    emotions = {
                        "dominant": dominant,
                        "values": emotions_filtered,
                    }
        except Exception:
            emotions = None

        animation_state["emotions"] = emotions

        message = {
            "type": "animation_state",
            "state": current.get("state") or getattr(state, "value", state),
            "descriptor": current.get("descriptor"),
            "started_at": current.get("started_at"),
            "animation_state": animation_state,
        }

        for session_id, websocket in list(self.connections.items()):
            try:
                await websocket.send_json(message)
            except Exception as exc:
                log_warning(
                    f"{LOG_PREFIX} Failed to broadcast animation state to {session_id}: {exc}"
                )

    async def get_animation_state(self):
        """HTTP endpoint that returns a lightweight animation state summary.

        This endpoint is used by clients to query the current canonical
        animation state tuple (state name, descriptor id, started_at).
        """
        try:
            if not self.animation_handler:
                return JSONResponse(
                    {"state": "idle", "descriptor": None, "started_at": None}
                )

            current = self.animation_handler.get_current_animation_state()
            payload = {
                "state": current.get("state"),
                "descriptor": current.get("descriptor"),
                "started_at": current.get("started_at"),
            }
            return JSONResponse(payload)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} get_animation_state failed: {exc}")
            raise HTTPException(
                status_code=500, detail=f"Failed to retrieve animation state: {exc}"
            ) from exc

    def _normalize_webui_attachment(self, attachment: dict[str, Any]) -> dict[str, Any]:
        """Normalize WebUI attachment metadata for local engine ingestion."""
        if not isinstance(attachment, dict):
            return attachment

        url = attachment.get("url")
        if not isinstance(url, str):
            return attachment

        parsed = urlparse(url)
        if parsed.path.startswith("/uploads/"):
            file_name = Path(unquote(parsed.path[len("/uploads/") :])).name
            if file_name:
                local_path = self.attachments_dir / file_name
                normalized = dict(attachment)
                normalized["path"] = str(local_path)
                normalized["file_path"] = str(local_path)
                if local_path.exists() and local_path.is_file():
                    try:
                        content = local_path.read_bytes()
                        normalized["data"] = base64.b64encode(content).decode("utf-8")
                        normalized["mime_type"] = normalized.get(
                            "mime_type",
                            mimetypes.guess_type(str(local_path))[0]
                            or "application/octet-stream",
                        )
                        normalized["size"] = normalized.get("size", len(content))
                    except Exception as exc:
                        log_warning(
                            f"{LOG_PREFIX} Failed to inline chat attachment data: {exc}"
                        )
                log_debug(
                    f"{LOG_PREFIX} Normalized webui attachment: filename={file_name}, "
                    f"mime_type={normalized.get('mime_type')}, "
                    f"size={normalized.get('size')}, "
                    f"path={normalized.get('path')}, "
                    f"inlined_data={'data' in normalized}"
                )
                return normalized

        return attachment

    async def _handle_user_message(
        self,
        session_id: str,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        is_voice_input: bool = False,
    ) -> None:
        from types import SimpleNamespace
        from core.config import TRAINER_NAME
        from core import message_queue

        normalized_attachments = [
            self._normalize_webui_attachment(att) for att in (attachments or [])
        ]

        log_info(
            f"{LOG_PREFIX} [_handle_user_message] START: session_id={session_id}, text_len={len(text)}, text={text[:100]}"
        )
        log_debug(
            f"{LOG_PREFIX} [_handle_user_message] normalized_attachments={len(normalized_attachments)}"
        )
        for att in normalized_attachments:
            log_debug(
                f"{LOG_PREFIX} [_handle_user_message] attachment metadata: "
                f"filename={att.get('filename')}, mime_type={att.get('mime_type')}, "
                f"size={att.get('size')}, path={att.get('path')}, "
                f"has_data={'data' in att}"
            )

        # Get trainer name for the user
        trainer_name = (
            str(TRAINER_NAME)
            if TRAINER_NAME and TRAINER_NAME != "Trainer"
            else "Trainer"
        )

        message = SimpleNamespace(
            chat_id=session_id,
            interface_path=f"{INTERFACE_NAME}/{session_id}",  # Add interface_path for proper routing
            message_id=int(datetime.utcnow().timestamp() * 1000) % 1_000_000,
            text=text,
            attachments=normalized_attachments or [],
            is_voice_input=is_voice_input,
            date=datetime.utcnow(),
            from_user=SimpleNamespace(
                id=session_id,
                username=trainer_name,
                first_name=trainer_name,
                last_name="",
                full_name=trainer_name,
            ),
            chat=SimpleNamespace(
                id=session_id,
                type="web",
                title=f"{BRAND_NAME} Session",
                full_name=f"{BRAND_NAME} Session",
            ),
            reply_to_message=None,
        )

        log_debug(f"{LOG_PREFIX} message from {session_id}: {text}")

        # NOTE: WebUI history is tracked via `_append_history()` which is wired
        # to the centralized context manager deque in `_ensure_session_history_loaded()`.

        # Global action ID for this message
        action_id = f"webui_msg_{session_id}_{message.message_id}"

        thinking_pushed = False
        try:
            # Push THINKING action to global state
            log_info(f"{LOG_PREFIX} Pushing THINKING action: {action_id}")
            thinking_pushed = await self.action_state_manager.push_action(
                action_id=action_id,
                phase=AnimationPhase.THINKING,
                component=INTERNAL_CHAT_NAME,
            )
            if not thinking_pushed:
                log_warning(
                    f"{LOG_PREFIX} THINKING action was rejected (lower priority than current action)"
                )

            # Set avatar animation to 'think'
            if self.persona_manager:
                try:
                    await self.persona_manager.set_animation_state(
                        "think", session_id=session_id
                    )
                    log_debug(
                        f"{LOG_PREFIX} Set avatar animation to 'think' for session {session_id}"
                    )
                except Exception as anim_exc:
                    log_warning(
                        f"{LOG_PREFIX} Failed to set 'think' animation: {anim_exc}"
                    )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Failed to push action state: {exc}")

        # If THINKING was pushed, keep it pending until the async response is sent.
        if thinking_pushed:
            self._pending_thinking_actions.setdefault(session_id, deque()).append(
                action_id
            )
            try:
                self._thinking_started_at_ms[session_id] = int(
                    datetime.utcnow().timestamp() * 1000
                )
            except Exception:
                pass

        # Get the configured response timeout from message_chain

        timeout_seconds = int(RESPONSE_TIMEOUT)

        try:
            # Enqueue message in the priority queue instead of processing directly
            # The message_queue consumer will handle it and send response via the interface
            log_info(
                f"{LOG_PREFIX} Enqueueing message to priority queue (skip_mention_check=True for WebUI)"
            )

            # Send immediate acknowledgement to client that message was received
            try:
                websocket = self.connections.get(session_id)
                if websocket:
                    ack_message = {
                        "type": "message_ack",
                        "message_id": message.message_id,
                        "status": "received",
                    }
                    await websocket.send_json(ack_message)
                    log_info(f"{LOG_PREFIX} Sent immediate ACK to session {session_id}")
            except Exception as ack_exc:
                log_warning(f"{LOG_PREFIX} Failed to send ACK message: {ack_exc}")

            # Mark session as processing in session_meta so clients can persist typing across views
            try:
                from core.session_meta import (
                    get_session_meta as get_meta_fn,
                    set_session_meta as set_meta_fn,
                )

                interface_path = f"{INTERFACE_NAME}/{session_id}"
                existing_meta = await get_meta_fn(interface_path) or {}
                existing_meta["processing"] = True
                await set_meta_fn(interface_path, existing_meta)
            except Exception as e:
                log_debug(
                    f"{LOG_PREFIX} Failed to set session processing meta before enqueue: {e}"
                )

            await message_queue.enqueue(
                bot=self,
                message=message,
                context_memory=None,
                priority=False,  # Normal priority for user messages
                interface_id=INTERFACE_NAME,
                skip_mention_check=True,  # WebUI is 1:1 interface, skip mention check
                original_message=message,
            )
            log_info(
                f"{LOG_PREFIX} Message successfully enqueued for session {session_id}"
            )

            # For WebUI, we don't wait for a direct response here.
            # The response will be sent via WebSocket by the message_queue consumer.
            # Set response to None to indicate the message was enqueued
            response = None

        except asyncio.TimeoutError:
            log_error(
                f"{LOG_PREFIX} Message enqueueing timed out after {timeout_seconds}s for session {session_id}"
            )
            response = str(get_failed_message_text())
        except Exception as exc:  # pragma: no cover - runtime issues
            log_error(f"{LOG_PREFIX} error enqueueing message: {exc}")
            response = str(get_failed_message_text())

        # Handle LLM_FAILED responses - use fallback message text
        # LLM_FAILED means the message_chain already sent fallback to other interfaces,
        # but for WebUI we need to send it here
        if response == "LLM_FAILED":
            response = str(get_failed_message_text())

        # For the normal WebUI async flow, response is None and the message_queue
        # will later call send_message/execute_action to deliver the response.
        # In that case we keep THINKING active and let send_message transition to
        # WRITE and then IDLE when the response is actually sent.
        if response:
            try:
                await self.send_message(session_id, text=response)
            except Exception as send_exc:
                log_warning(
                    f"{LOG_PREFIX} Failed to send response to session {session_id}: {send_exc}"
                )

    async def _replay_history(
        self, session_id: str, websocket: Optional["WebSocket"] = None
    ) -> None:
        history = self.message_history.get(session_id)
        if not history:
            log_debug(
                f"{LOG_PREFIX} _replay_history: no history for session {session_id}"
            )
            return
        # A single logical session may be watched by several spectator sockets
        # registered under keys like ``webui_default:<hex>`` (single-session
        # mode). ``self.connections.get(session_id)`` would miss those, so when
        # no explicit socket is supplied resolve the first live socket for the
        # logical session. Replay is per-connection to avoid duplicating the
        # history on clients that are already connected.
        if websocket is None:
            targets = self._sockets_for_session(session_id)
            websocket = targets[0][1] if targets else None
        if not websocket:
            log_debug(
                f"{LOG_PREFIX} _replay_history: no websocket for session {session_id}"
            )
            return
        for item in history:
            # Karada 3D-interaction events are recorded into chat history purely
            # for LLM context; they must never surface as visible chat bubbles.
            try:
                _meta = item.get("metadata") if isinstance(item, dict) else None
                if isinstance(_meta, dict) and _meta.get("karada_touch"):
                    continue
            except Exception:
                pass
            # Normalize history item to expected format: {type:'message', sender:'synth'|'user', text: '...'}
            try:
                sender = item.get("sender") if isinstance(item, dict) else None
            except Exception:
                sender = None
            if not sender:
                # Try common keys from chat_history_cache / context_manager
                if isinstance(item, dict):
                    sname = item.get("sender_name") or item.get("username") or None
                    # Normalize commonly used names for the SyntH agent to 'synth'
                    if sname and str(sname).lower() in (
                        "self",
                        "synth",
                        "bot",
                        "system",
                        "synth_webui",
                    ):
                        sender = "synth"
                    else:
                        sender = "user"
                else:
                    sender = "synth"
            text = item.get("text") if isinstance(item, dict) else str(item)
            replay_payload: Dict[str, Any] = {
                "type": "message",
                "sender": sender,
                "text": text,
            }
            # Propagate metadata fields (e.g. tts_url) so the client can restore
            # click-to-replay audio icons on reconnect / restart.
            meta = item.get("metadata") if isinstance(item, dict) else None
            if isinstance(meta, dict):
                tts_url = meta.get("tts_url")
                if tts_url:
                    replay_payload["tts_url"] = tts_url
                    replay_payload["data"] = {"tts_url": tts_url}
                else:
                    replay_payload["data"] = meta
                attachments = meta.get("attachments")
                if attachments:
                    replay_payload["attachments"] = attachments
            await websocket.send_json(replay_payload)
        log_info(
            f"{LOG_PREFIX} _replay_history: sent {len(history)} messages to session {session_id}"
        )

    async def _append_history(
        self,
        session_id: str,
        sender: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        skip_history: bool = False,
    ) -> None:
        history = self.message_history.setdefault(
            session_id, deque(maxlen=self.max_history)
        )

        # Store in the same schema used by the centralized context manager so
        # HistoryEngine can format `history_current_chat` consistently.
        from datetime import datetime

        interface_path = f"{INTERFACE_NAME}/{session_id}"

        canonical_sender = sender
        try:
            if isinstance(sender, str) and sender.lower() in (
                "synth",
                "bot",
                "synth_webui",
            ):
                canonical_sender = "self"
        except Exception:
            canonical_sender = sender

        from datetime import timezone

        msg: dict[str, Any] = {
            "message_id": None,
            "user_id": "self" if canonical_sender == "self" else str(session_id),
            "username": canonical_sender,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "interface_path": interface_path,
        }
        if metadata and isinstance(metadata, dict):
            msg["metadata"] = metadata
        history.append(msg)

        # Persist to chat_history_cache for long-term storage
        if not skip_history:
            try:
                from core.chat_history_cache import save_chat_message

                # Normalize sender_name for DB storage: we want to store "self" as the
                # canonical name for the SyntH agent so that restore/replay can map
                # it back to "synth" for WS payloads. This avoids misattribution
                # where stored value "synth" would be considered a user on replay.
                db_sender_name = sender
                try:
                    if isinstance(sender, str) and sender.lower() in (
                        "synth",
                        "bot",
                        "synth_webui",
                    ):
                        db_sender_name = "self"
                except Exception:
                    db_sender_name = sender

                await save_chat_message(
                    interface_path,
                    text,
                    sender_name=db_sender_name,
                    sender_id=session_id,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            except Exception as e:
                log_debug(
                    f"{LOG_PREFIX} Failed to persist chat message for {session_id}: {e}"
                )

    def _logical_session_id(self, conn_key: str) -> str:
        """Map a physical connection key back to its logical session id.

        In single-session mode a socket is registered under a composite key
        ``"<session_id>:<socket-uuid>"`` so that multiple spectator clients can
        coexist without overwriting each other (Karada v2 shared-state model).
        This strips the ``:<socket-uuid>`` suffix to recover the shared logical
        ``session_id`` used for chat history / interface_path. In multi-session
        mode keys have no suffix and are returned unchanged.
        """
        return conn_key.split(":", 1)[0] if ":" in conn_key else conn_key

    def _sockets_for_session(self, session_id: str) -> "list[tuple[str, WebSocket]]":
        """Return all live ``(conn_key, websocket)`` pairs for a logical session.

        A logical session may be observed by several spectator clients that each
        hold their own physical socket. Outbound messages for a shared avatar
        (text, audio, animation) must reach every one of them, so callers should
        iterate this list instead of doing a single ``connections.get(id)``.
        """
        return [
            (key, ws)
            for key, ws in self.connections.items()
            if self._logical_session_id(key) == session_id
        ]

    def _multi_session_enabled(self) -> bool:
        """Return True if the experimental multi-session flag is active.

        This flag is exposed via a configurable variable; reading it is cheap
        so we query the registry on each call to ensure runtime toggles take
        effect (even though the feature is mostly intended to be set once)."""
        try:
            return bool(
                config_registry.get_value(
                    "MULTI_SESSION",
                    False,
                    value_type=bool,
                    group="core",
                    component="webui",
                )
            )
        except Exception:
            return False

    async def _load_session_id_from_db(self) -> "str | None":
        """Read the WebUI session ID from the ``config`` table.

        Returns the stored value, or ``None`` if unavailable (DB offline, key absent).
        The file-based fallback in :meth:`_ensure_persistent_session_id` covers that case.
        """
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT value FROM config WHERE config_key = 'webui_session_id' LIMIT 1"
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        sid = str(row[0]).strip()
                        if sid:
                            log_debug(f"{LOG_PREFIX} Loaded session id from DB: {sid}")
                            return sid
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} Could not load session id from DB: {exc}")
        return None

    async def _save_session_id_to_db(self, sid: str) -> None:
        """Persist the WebUI session ID to the ``config`` table (upsert)."""
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO config (config_key, value) VALUES ('webui_session_id', %s) "
                        "ON DUPLICATE KEY UPDATE value = VALUES(value)",
                        (sid,),
                    )
                await conn.commit()
                log_info(f"{LOG_PREFIX} Saved session id to DB: {sid}")
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} Could not save session id to DB: {exc}")

    def _ensure_persistent_session_id(self, force_write: bool = False) -> None:
        """Synchronous initialisation of the single-session ID.

        In single-session mode (MULTI_SESSION=False, default) we always use the
        fixed constant ``"webui_default"`` as the session identifier.  This makes
        chat history survive container restarts without depending on file or DB
        persistence.  The async DB-backed path is no longer called on reconnect.
        """
        try:
            if self._multi_session_enabled():
                log_debug(
                    f"{LOG_PREFIX} MULTI_SESSION enabled; skipping persistent session file"
                )
                return
            # Fixed constant for single-user deployments — no UUID generation needed.
            self.session_id = "webui_default"
            log_debug(
                f"{LOG_PREFIX} Single-session mode: using fixed session id 'webui_default'"
            )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Failed to ensure persistent session id: {exc}")

    def _schedule_uploads_cleanup(self) -> None:
        """Schedule the temporary animation uploads cleanup loop."""
        if self._uploads_cleanup_task and not self._uploads_cleanup_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._uploads_cleanup_task = loop.create_task(
                    self._cleanup_animation_uploads_loop()
                )
            else:
                self._uploads_cleanup_task = asyncio.create_task(
                    self._cleanup_animation_uploads_loop()
                )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Failed to schedule uploads cleanup loop: {exc}")

    async def _cleanup_animation_uploads_loop(self) -> None:
        """Periodically remove expired temporary animation uploads."""
        while True:
            try:
                removed = animation_uploads.cleanup_expired_uploads(
                    self.mate_upload_ttl_days
                )
                if removed and getattr(self, "animation_handler", None):
                    for upload_id in removed:
                        animations_root = (
                            animation_uploads.get_upload_root(upload_id) / "animations"
                        )
                        self.animation_handler.remove_temporary_search_path(
                            animations_root
                        )
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Uploads cleanup loop failed: {exc}")
            await asyncio.sleep(max(self.mate_upload_cleanup_interval_s, 60))

    async def _ensure_session_history_loaded(self, session_id: str) -> None:
        """Load persisted chat history for the given session into self.message_history.

        This keeps the LLM context rehydration path intact, but restores the
        WebUI-visible session history from the persisted cache using the WebUI's
        own max_history limit instead of the smaller prompt-context deque.
        """
        try:
            from core.chat_context_manager import load_chat_history as load_context
            from core.chat_history_cache import load_chat_history as load_persisted

            interface_path = f"{INTERFACE_NAME}/{session_id}"
            await load_context(interface_path)
            persisted_history = await load_persisted(
                interface_path,
                limit=self.max_history,
            )
            self.message_history[session_id] = deque(
                persisted_history,
                maxlen=self.max_history,
            )
            log_debug(
                f"{LOG_PREFIX} Session history for {session_id} loaded, {len(self.message_history[session_id])} messages"
            )
        except Exception as e:
            log_debug(
                f"{LOG_PREFIX} Unable to load session history for {session_id}: {e}"
            )

    # ------------------------------------------------------------------
    # Methods used by actions / plugins
    # ------------------------------------------------------------------
    async def send_message(
        self,
        payload_or_chat_id=None,
        text: Optional[str] = None,
        **kwargs,
    ) -> None:
        skip_history = kwargs.pop("skip_history", False)
        metadata = kwargs.pop("metadata", None)
        # Normalize metadata to a dict, otherwise ignore it.
        if metadata is not None and not isinstance(metadata, dict):
            metadata = None

        if isinstance(payload_or_chat_id, dict):
            payload = payload_or_chat_id
            # Accept both "text" (standard) and "value" (legacy synthetic-action mapping)
            payload_text = (
                payload.get("text") or payload.get("value") or payload.get("content")
            )
            if payload_text is not None:
                text = payload_text
            chat_id = (
                payload.get("interface_path")
                or payload.get("target")
                or payload.get("chat_id")
            )
            skip_history = payload.get("skip_history", skip_history)
            if metadata is None and isinstance(payload.get("metadata"), dict):
                metadata = payload.get("metadata")
        else:
            chat_id = payload_or_chat_id or kwargs.get("chat_id")
            if text is None:
                text = kwargs.get("text")
            if metadata is None and isinstance(kwargs.get("metadata"), dict):
                metadata = kwargs.get("metadata")

        # Guard against accidental object leakage (e.g. original_message passed as
        # positional arg by callers). Only strings are valid outbound message text.
        if text is not None and not isinstance(text, str):
            log_warning(
                f"{LOG_PREFIX} send_message got non-string text type={type(text).__name__}; dropping"
            )
            return

        if not text or not chat_id:
            log_warning(f"{LOG_PREFIX} send_message missing text or chat_id")
            return

        # Handle interface_path format: extract session_id from "synth_webui/session_id"
        if "/" in str(chat_id):
            parts = str(chat_id).split("/")
            if len(parts) >= 2 and parts[0] == INTERFACE_NAME:
                session_id = parts[1]
                log_debug(
                    f"{LOG_PREFIX} Extracted session {session_id} from interface_path {chat_id}"
                )
                chat_id = session_id

        session_id = str(chat_id)
        # Karada v2 shared-state: a logical session may be watched by several
        # spectator sockets. Collect *all* of them so the message reaches every
        # viewer, not just the one whose key happens to equal session_id.
        targets = self._sockets_for_session(session_id)
        websocket = targets[0][1] if targets else None
        if not websocket:
            try:
                from core.config_manager import config_registry

                alias_map = config_registry.get_value(
                    "CONTEXT_LINK_MAP", {}, value_type="json"
                )
                if isinstance(alias_map, dict):
                    sources = [k for k, v in alias_map.items() if v == session_id]
                    for source in sources:
                        candidate = None
                        if source in self.connections:
                            candidate = source
                        elif "/" in source and source.startswith(f"{INTERFACE_NAME}/"):
                            extracted = source.split("/")[1]
                            if extracted in self.connections:
                                candidate = extracted

                        if candidate:
                            log_debug(
                                f"{LOG_PREFIX} 🔀 Alias redirect: {session_id} -> {candidate}"
                            )
                            session_id = self._logical_session_id(candidate)
                            targets = self._sockets_for_session(session_id)
                            websocket = targets[0][1] if targets else None
                            break
            except Exception as e:
                log_debug(f"{LOG_PREFIX} Alias resolution check failed: {e}")
        if not websocket:
            # Improved debug information: list active sessions to help debug target mismatches
            active_sessions = list(self.connections.keys())
            log_warning(
                f"{LOG_PREFIX} no active websocket for session {chat_id}. Active sessions: {active_sessions}"
            )
            log_debug(
                f"{LOG_PREFIX} send_message payload target: {chat_id}, text length: {len(text) if text else 0}"
            )
            # Do not return: persist the message so it will be visible on reconnect
            websocket = None

        # Ensure any pending THINKING is cleared before delivery (fallback).
        await self._webui_clear_pending_thinking(session_id)

        # Reuse the WRITING action started by on_generation_start (if any).
        # Do NOT push a new WRITING here: that would cause a spurious WRITING→IDLE
        # flash for auxiliary/follow-up sends (e.g. grillo checker actions) that
        # arrive after the primary LLM response has already been delivered.
        # The typing indicator is exclusively managed by on_generation_start /
        # on_generation_end; send_message only cleans it up when it was active.
        writing_action_id = None
        writing_pushed = False
        existing_writing = self._active_writing_actions.get(session_id)
        if existing_writing and len(existing_writing) > 0:
            writing_action_id = existing_writing[-1]
            writing_pushed = True

        # Normalize metadata before websocket/history/DB use to avoid serialization errors.
        safe_metadata = self._clean_for_json(metadata) if metadata is not None else None

        # If websocket is present attempt to send; otherwise persist for later replay.
        # Broadcast to every spectator socket of this logical session so all
        # viewers of the shared avatar see the same message bubble.
        if websocket:
            payload: Dict[str, Any] = {
                "type": "message",
                "sender": "synth",
                "text": text,
            }
            # Forward attachments if present so the WebUI can render them.
            if metadata and isinstance(metadata.get("attachments"), list):
                payload["attachments"] = metadata["attachments"]
                # Keep attachments accessible under `data` for compatibility.
                payload.setdefault("data", {})["attachments"] = metadata["attachments"]

            # Forward metadata fields that the client can use (e.g. tts_url).
            if safe_metadata and safe_metadata.get("tts_url"):
                payload["tts_url"] = safe_metadata["tts_url"]
                payload.setdefault("data", {})["tts_url"] = safe_metadata["tts_url"]

            clean_payload = self._clean_for_json(payload)
            for target_key, target_ws in targets:
                try:
                    await target_ws.send_json(clean_payload)
                except Exception as e:
                    log_warning(
                        f"{LOG_PREFIX} Failed to send websocket message to {target_key}: {e}"
                    )

        # Append to in-memory history so reconnect will replay it
        await self._append_history(
            session_id, "synth", text, metadata=safe_metadata, skip_history=skip_history
        )

        # Save SyntH's response via core chat_context_manager
        if not skip_history:
            try:
                from core.chat_context_manager import save_response_message

                msg_interface_path = f"{INTERFACE_NAME}/{chat_id}"
                await save_response_message(
                    msg_interface_path, text, metadata=safe_metadata
                )
            except Exception as e:
                log_debug(
                    f"{LOG_PREFIX} Failed to save response via context_manager: {e}"
                )

        if websocket:
            log_info(
                f"{LOG_PREFIX} Sent message to session {session_id}: {text[:80]}{'...' if len(text) > 80 else ''}"
            )
        else:
            log_info(
                f"{LOG_PREFIX} Saved message for session {session_id} (no active websocket): {text[:80]}{'...' if len(text) > 80 else ''}"
            )
        # Transition: WRITE -> IDLE (always, once message is sent)
        if writing_pushed and writing_action_id:
            try:
                await self.action_state_manager.pop_action(writing_action_id)
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Failed to pop WRITING action state: {exc}")
            # Remove from per-session registry
            try:
                dq = self._active_writing_actions.get(session_id)
                if dq and writing_action_id in dq:
                    try:
                        dq.remove(writing_action_id)
                    except Exception:
                        # Fallback: rebuild without the id
                        self._active_writing_actions[session_id] = deque(
                            [x for x in dq if x != writing_action_id]
                        )
                if dq is not None and len(dq) == 0:
                    self._active_writing_actions.pop(session_id, None)
            except Exception:
                pass

            # Always return to IDLE after WRITING is popped (don't conditionally check phase)
            if self.persona_manager:
                try:
                    await self.persona_manager.set_animation_state(
                        "idle", session_id=session_id
                    )
                except Exception as anim_exc:
                    log_debug(
                        f"{LOG_PREFIX} Failed to set 'idle' animation after send: {anim_exc}"
                    )

        # Clear processing meta now that we've delivered a response
        try:
            from core.session_meta import (
                get_session_meta as get_meta_fn,
                set_session_meta as set_meta_fn,
            )

            interface_path = f"{INTERFACE_NAME}/{session_id}"
            existing_meta = await get_meta_fn(interface_path) or {}
            existing_meta["processing"] = False
            await set_meta_fn(interface_path, existing_meta)
        except Exception as e:
            log_debug(
                f"{LOG_PREFIX} Failed to clear session processing meta after send: {e}"
            )

    async def send_tts_audio(
        self,
        session_id: str,
        audio_path: str,
        text: Optional[str] = None,
        lipsync_data: Optional[Dict[str, Any]] = None,
        audio_duration_s: Optional[float] = None,
    ) -> bool:
        """Push a TTS audio playback event to a WebUI session.

        Called by :class:`~plugins.vox_plugin.VoxPlugin` immediately after the
        audio file has been written to disk.  The client will auto-play the
        audio and attach click-to-replay functionality to the last synth
        message bubble.  The URL is derived from *audio_path* so it is
        accessible via the ``/static`` mount.

        Args:
            session_id:      WebUI session identifier (plain or ``synth_webui/<id>`` form).
            audio_path:      Absolute or relative filesystem path to the audio file.
            text:            Optional caption / message text (for accessibility).
            lipsync_data:    Optional phoneme/timing dict forwarded to the animator.
            audio_duration_s: Duration of the audio in seconds (used by the client
                              to synchronise facial expressions with playback).

        Returns:
            ``True`` if the message was delivered, ``False`` if no websocket was found.
        """
        # Normalise session_id – strip "synth_webui/" prefix if present
        sid = str(session_id)
        if "/" in sid:
            parts = sid.split("/")
            if len(parts) >= 2 and parts[0] == INTERFACE_NAME:
                sid = parts[1]

        if not self._sockets_for_session(sid):
            log_warning(f"{LOG_PREFIX} send_tts_audio: no websocket for session {sid}")
            return False

        # Derive a client-accessible URL from the filesystem path.
        # Audio is stored under the /static mount, e.g.
        #   res/synth_webui/static/audio/tts/vox_123.wav → /static/audio/tts/vox_123.wav
        try:
            from pathlib import Path as _Path

            p = _Path(audio_path)
            parts_list = list(p.parts)
            try:
                idx = parts_list.index("static")
                url = "/" + "/".join(parts_list[idx:])
            except ValueError:
                url = "/static/audio/tts/" + p.name
        except Exception:
            url = "/static/audio/tts/" + str(audio_path).rsplit("/", 1)[-1]

        # Deliver the caption as a regular chat message so that it is persisted
        # in the DB, appears in the in-memory history (replay on reconnect) and
        # shows as a visible text bubble on clients that may not support audio.
        # Attach ``tts_url`` in the metadata so the click-to-replay audio icon
        # is bound directly to the bubble on *every* spectator client (and
        # restored on reconnect) without depending on the transient
        # ``tts-play`` event race.
        if text:
            try:
                await self.send_message(sid, text=text, metadata={"tts_url": url})
            except Exception as exc:
                log_warning(
                    f"{LOG_PREFIX} send_tts_audio: failed to send caption message for session {sid}: {exc}"
                )

        # NOTE: the actual ``tts-play`` broadcast to all clients and the
        # ``set_current_audio`` bookkeeping for late-joiners are performed by
        # the Karada state server (the single source of truth for "the avatar
        # is speaking"), which the Vox plugin drives directly. This method only
        # persists/renders the WebUI-specific chat caption above so we do NOT
        # broadcast the audio again here (that would double-play the clip).
        return True

    async def _webui_clear_pending_thinking(self, session_id: str) -> None:
        pending = self._pending_thinking_actions.get(session_id)
        if not pending:
            return
        while pending:
            pending_action_id = pending.popleft()
            try:
                await self.action_state_manager.pop_action(pending_action_id)
            except Exception as exc:
                log_warning(
                    f"{LOG_PREFIX} Failed to pop pending THINKING action {pending_action_id}: {exc}"
                )
        self._pending_thinking_actions.pop(session_id, None)

    async def _pop_latest_pending_thinking_action(
        self, session_id: str
    ) -> Optional[str]:
        pending = self._pending_thinking_actions.get(session_id)
        if not pending:
            return None
        while len(pending) > 1:
            stale_action = pending.popleft()
            try:
                await self.action_state_manager.pop_action(stale_action)
            except Exception as exc:
                log_warning(
                    f"{LOG_PREFIX} Failed to pop stale THINKING action {stale_action}: {exc}"
                )
        action_id = pending.pop() if pending else None
        self._pending_thinking_actions.pop(session_id, None)
        return action_id

    async def on_generation_start(self, interface_path: str, **kwargs) -> None:
        """Optional hook called by the queue when processing starts.

        For WebUI this approximates 'LLM started responding', so we switch THINK->WRITE
        as early as possible (before the final message is sent).

        For voice-originated input (Auris STT) there is no textual writing phase —
        the reply is spoken — so the avatar stays in THINK during generation.
        """
        # Detect voice-originated input; if so we keep THINK instead of WRITE.
        _is_voice_input = False
        try:
            _ctx = kwargs.get("context")
            if isinstance(_ctx, dict) and _ctx.get("is_voice_input"):
                _is_voice_input = True
            _msg = kwargs.get("message")
            if not _is_voice_input and getattr(_msg, "is_voice_input", False):
                _is_voice_input = True
        except Exception:
            _is_voice_input = False

        # Switch THINK -> WRITE when generation actually starts, but ensure THINK
        # remains visible for a short minimum window to avoid being skipped.
        try:
            session_id = None
            if interface_path and "/" in str(interface_path):
                parts = str(interface_path).split("/")
                if len(parts) >= 2 and parts[0] == INTERFACE_NAME:
                    session_id = parts[1]
            if not session_id:
                return

            # Ensure THINK is visible for at least this long before switching.
            min_think_ms = 450
            started_ms = self._thinking_started_at_ms.get(session_id)
            if isinstance(started_ms, int) and started_ms > 0:
                now_ms = int(datetime.utcnow().timestamp() * 1000)
                remaining = max(0, (started_ms + min_think_ms) - now_ms)
                if remaining > 0:
                    await asyncio.sleep(remaining / 1000)

            # Start WRITING if not already active for this session.
            existing_writing = self._active_writing_actions.get(session_id)
            writing_action_id = None
            writing_pushed = False
            if existing_writing and len(existing_writing) > 0:
                writing_action_id = existing_writing[-1]
                writing_pushed = True
            else:
                pending_action_id = await self._pop_latest_pending_thinking_action(
                    session_id
                )
                if pending_action_id:
                    try:
                        writing_pushed = await self.action_state_manager.update_phase(
                            pending_action_id,
                            AnimationPhase.WRITING,
                        )
                    except Exception as exc:
                        log_warning(
                            f"{LOG_PREFIX} Failed to promote THINKING to WRITING: {exc}"
                        )
                        writing_pushed = False
                    if writing_pushed:
                        writing_action_id = pending_action_id
                        self._active_writing_actions.setdefault(
                            session_id, deque()
                        ).append(writing_action_id)
                    else:
                        try:
                            await self.action_state_manager.pop_action(
                                pending_action_id
                            )
                        except Exception:
                            pass
            if not writing_pushed:
                writing_action_id = f"webui_write_{session_id}_{int(datetime.utcnow().timestamp() * 1000) % 1_000_000}"
                try:
                    writing_pushed = await self.action_state_manager.push_action(
                        action_id=writing_action_id,
                        phase=AnimationPhase.WRITING,
                        component=INTERNAL_CHAT_NAME,
                    )
                except Exception as exc:
                    log_warning(
                        f"{LOG_PREFIX} Failed to push WRITING action state (generation_start): {exc}"
                    )
                    writing_pushed = False

                if writing_pushed:
                    self._active_writing_actions.setdefault(session_id, deque()).append(
                        writing_action_id
                    )

            if writing_pushed and self.persona_manager:
                # Voice input has no textual writing phase — keep the avatar in
                # THINK during generation (the reply is spoken via Vox).
                _anim_state = "think" if _is_voice_input else "write"
                try:
                    await self.persona_manager.set_animation_state(
                        _anim_state, session_id=session_id
                    )
                except Exception as anim_exc:
                    log_debug(
                        f"{LOG_PREFIX} Failed to set '{_anim_state}' animation (generation_start): {anim_exc}"
                    )
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} on_generation_start failed: {exc}")
        return

    async def on_generation_end(
        self, interface_path: str, success: bool = True, **kwargs
    ) -> None:
        """Optional hook called by the queue when processing ends.

        For WebUI this is the earliest reliable signal that generation is finished.
        If the response path didn't go through send_message (errors/plugins), we still
        want to stop WRITING and return to IDLE when appropriate.
        """
        try:
            session_id = None
            if interface_path and "/" in str(interface_path):
                parts = str(interface_path).split("/")
                if len(parts) >= 2 and parts[0] == INTERFACE_NAME:
                    session_id = parts[1]
            if not session_id:
                return

            # Pop any active WRITING actions for this session.
            try:
                dq = self._active_writing_actions.get(session_id)
            except Exception:
                dq = None

            if dq:
                for action_id in list(dq):
                    try:
                        await self.action_state_manager.pop_action(action_id)
                    except Exception:
                        pass
                self._active_writing_actions.pop(session_id, None)

            # Only set idle if nothing higher-priority is active.
            if self.persona_manager:
                try:
                    current_phase = await self.action_state_manager.get_current_phase()
                    if current_phase == AnimationPhase.IDLE:
                        await self.persona_manager.set_animation_state(
                            "idle", session_id=session_id
                        )
                except Exception as anim_exc:
                    log_debug(
                        f"{LOG_PREFIX} Failed to set 'idle' animation (generation_end): {anim_exc}"
                    )
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} on_generation_end failed: {exc}")

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        if action.get("type") == "message_synth_webui":
            payload = action.get("payload", {})

            def _extract_session_id(value: str | None) -> str | None:
                if not value or "/" not in str(value):
                    return None
                parts = str(value).split("/")
                if len(parts) >= 2 and parts[0] == INTERFACE_NAME:
                    return parts[1]
                return None

            session_id = context.get("chat_id")
            if not session_id:
                session_id = _extract_session_id(context.get("interface_path"))
            if not session_id:
                session_id = _extract_session_id(payload.get("interface_path"))
            if not session_id and original_message is not None:
                session_id = _extract_session_id(
                    getattr(original_message, "interface_path", None)
                )

            # Ensure the payload has the correct interface_path for sending
            if session_id:
                payload["interface_path"] = f"{INTERFACE_NAME}/{session_id}"
            await self.send_message(payload, original_message=original_message)
        elif action.get("type") in ("message_mate_engine", "message_integration"):
            payload = action.get("payload", {})
            text = payload.get("text")
            target = payload.get("target")
            source = (
                payload.get("source")
                if action.get("type") == "message_integration"
                else "mate"
            )
            if text:
                # Use the generic enqueue_outbox API for integration messages
                await self.enqueue_outbox(source, text=text, target=target)

    # ------------------------------------------------------------------
    # VRM management API
    # ------------------------------------------------------------------
    def _load_active_vrm(self) -> Optional[str]:
        log_debug(f"{LOG_PREFIX} Loading active VRM model...")
        log_debug(f"{LOG_PREFIX} VRM directory: {self.vrm_dir}")
        log_debug(f"{LOG_PREFIX} Active VRM marker file: {self.active_vrm_marker}")

        if self.active_vrm_marker.exists():
            try:
                name = self.active_vrm_marker.read_text(encoding="utf-8").strip()
                log_debug(f"{LOG_PREFIX} Found marker file with name: {name}")
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Failed to read marker file: {exc}")
                name = ""
            if name:
                candidate = self.vrm_dir / Path(name).name
                if candidate.exists():
                    log_info(
                        f"{LOG_PREFIX} Active VRM loaded from marker: {candidate.name}"
                    )
                    return candidate.name
                else:
                    log_warning(
                        f"{LOG_PREFIX} Marker references non-existent file: {candidate}"
                    )
        else:
            log_debug(
                f"{LOG_PREFIX} No marker file found, looking for first available VRM..."
            )

        # Fallback to first available model, preferring SynTh.vrm as default
        available_vrms = list(sorted(self.vrm_dir.glob("*.vrm")))
        log_debug(
            f"{LOG_PREFIX} Available VRM files in temp dir: {[v.name for v in available_vrms]}"
        )
        # clean up any stale files from previous versions; keep only model.vrm
        if len(available_vrms) > 1 and (self.vrm_dir / "model.vrm").exists():
            log_debug(f"{LOG_PREFIX} Cleaning stale VRM files at startup")
            for f in available_vrms:
                if f.name != "model.vrm":
                    try:
                        f.unlink()
                        log_debug(f"{LOG_PREFIX} removed stale VRM {f.name}")
                    except Exception as _:
                        log_warning(f"{LOG_PREFIX} failed to remove stale VRM {f}")
            available_vrms = [self.vrm_dir / "model.vrm"]

        # Prefer SyntH.vrm as the default model
        synth_vrm = self.vrm_dir / "SyntH.vrm"
        if synth_vrm.exists():
            log_info(f"{LOG_PREFIX} Using default SyntH.vrm model")
            self._set_active_vrm(synth_vrm.name)
            return synth_vrm.name

        # Prefer Rei model from skins/ only when there are NO user-uploaded VRMs
        # This prevents overriding a user's uploaded model when no marker is present.
        rei_vrm = Path(__file__).resolve().parent.parent / "skins" / "Rei" / "model.vrm"
        if not available_vrms and rei_vrm.exists():
            log_info(
                f"{LOG_PREFIX} Using Rei model from skins as default (no user VRMs present)"
            )
            try:
                web_path = rei_vrm.relative_to(Path(__file__).resolve().parent.parent)
                return f"/{web_path.as_posix()}"
            except ValueError:
                return "/skins/Rei/model.vrm"

        # Otherwise use first available from temp (user-uploaded VRMs take precedence)
        for candidate in available_vrms:
            log_info(
                f"{LOG_PREFIX} Using first available VRM from temp: {candidate.name}"
            )
            return candidate.name

        # Fallback: try to find a model in the current persona's folder
        try:
            from core.persona_manager import get_persona_manager

            persona_mgr = get_persona_manager()
            current_persona = persona_mgr.get_current_persona()
            if current_persona and current_persona.name:
                persona_folder = (
                    Path(__file__).resolve().parent.parent
                    / "skins"
                    / current_persona.name
                )
                persona_vrm = persona_folder / "model.vrm"
                if persona_vrm.exists():
                    log_info(
                        f"{LOG_PREFIX} Using VRM from current persona folder: {persona_vrm}"
                    )
                    # Return as relative URL from web root
                    try:
                        web_path = persona_vrm.relative_to(
                            Path(__file__).resolve().parent.parent
                        )
                        return f"/{web_path}"
                    except ValueError:
                        return f"/skins/{current_persona.name}/model.vrm"
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} Failed to find VRM from persona manager: {exc}")

        # Last resort: try common persona folders
        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        for persona_folder in ["Rei", "Zero"]:
            persona_vrm = skins_dir / persona_folder / "model.vrm"
            if persona_vrm.exists():
                log_info(
                    f"{LOG_PREFIX} Using fallback VRM from {persona_folder}: {persona_vrm}"
                )
                return f"/skins/{persona_folder}/model.vrm"

        log_warning(f"{LOG_PREFIX} No VRM models found in any location")
        return None

    def _set_active_vrm(self, model_name: Optional[str]) -> None:
        log_info(f"{LOG_PREFIX} ========== SET ACTIVE VRM START ==========")
        log_info(f"{LOG_PREFIX} Setting active VRM to: '{model_name}'")
        log_debug(f"{LOG_PREFIX} Current active VRM before change: '{self.active_vrm}'")
        log_debug(f"{LOG_PREFIX} Active VRM marker path: {self.active_vrm_marker}")
        log_debug(
            f"{LOG_PREFIX} Active VRM marker exists: {self.active_vrm_marker.exists() if hasattr(self, 'active_vrm_marker') else 'N/A'}"
        )

        if not model_name:
            log_info(f"{LOG_PREFIX} Clearing active VRM (model_name is None/empty)")
            try:
                if self.active_vrm_marker.exists():
                    log_debug(f"{LOG_PREFIX} Removing active VRM marker file...")
                    self.active_vrm_marker.unlink()
                    log_info(f"{LOG_PREFIX} ✓ Removed active VRM marker")
                else:
                    log_debug(
                        f"{LOG_PREFIX} Active VRM marker does not exist, nothing to remove"
                    )
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} ⚠️ Failed to remove marker: {exc}")
                import traceback

                log_warning(f"{LOG_PREFIX} Traceback: {traceback.format_exc()}")
            self.active_vrm = None
            log_info(f"{LOG_PREFIX} ✓ Active VRM cleared")
            log_info(f"{LOG_PREFIX} ========== SET ACTIVE VRM END (cleared) ==========")
            return

        log_debug(f"{LOG_PREFIX} Model name provided: '{model_name}'")
        log_debug(f"{LOG_PREFIX} Extracting basename from model_name...")
        basename = Path(model_name).name
        log_debug(f"{LOG_PREFIX} Basename extracted: '{basename}'")

        candidate = self.vrm_dir / basename
        log_info(f"{LOG_PREFIX} Full VRM candidate path: {candidate}")
        log_debug(f"{LOG_PREFIX} VRM directory: {self.vrm_dir}")
        log_debug(f"{LOG_PREFIX} VRM directory exists: {self.vrm_dir.exists()}")
        log_debug(f"{LOG_PREFIX} Candidate file exists: {candidate.exists()}")

        if not candidate.exists():
            log_error(f"{LOG_PREFIX} ⚠️ VRM file not found at: {candidate}")
            log_error(f"{LOG_PREFIX} Directory contents:")
            try:
                if self.vrm_dir.exists():
                    contents = list(self.vrm_dir.iterdir())
                    for item in contents:
                        log_error(
                            f"{LOG_PREFIX}   - {item.name} ({'file' if item.is_file() else 'dir'})"
                        )
                    if not contents:
                        log_error(f"{LOG_PREFIX}   (directory is empty)")
                else:
                    log_error(f"{LOG_PREFIX}   (directory does not exist)")
            except Exception as list_exc:
                log_error(f"{LOG_PREFIX} Failed to list directory: {list_exc}")

            log_info(
                f"{LOG_PREFIX} ========== SET ACTIVE VRM END (not found) =========="
            )
            raise FileNotFoundError(model_name)

        log_debug(f"{LOG_PREFIX} ✓ VRM file exists, writing marker...")
        log_debug(f"{LOG_PREFIX} Marker will contain: '{candidate.name}'")

        try:
            self.active_vrm_marker.write_text(candidate.name, encoding="utf-8")
            log_info(f"{LOG_PREFIX} ✓ Wrote marker file for: {candidate.name}")
            log_debug(
                f"{LOG_PREFIX} Marker file exists after write: {self.active_vrm_marker.exists()}"
            )
            if self.active_vrm_marker.exists():
                marker_content = self.active_vrm_marker.read_text(encoding="utf-8")
                log_debug(f"{LOG_PREFIX} Marker file content: '{marker_content}'")
        except Exception as exc:  # pragma: no cover - file system issues
            log_warning(f"{LOG_PREFIX} ⚠️ Failed to persist active VRM marker: {exc}")
            import traceback

            log_warning(f"{LOG_PREFIX} Traceback: {traceback.format_exc()}")

        self.active_vrm = candidate.name
        log_info(f"{LOG_PREFIX} ✓ Active VRM set to: '{self.active_vrm}'")
        log_info(f"{LOG_PREFIX} ========== SET ACTIVE VRM END (success) ==========")

    # ------------------------------------------------------------------
    # skin/VRM helpers
    # ------------------------------------------------------------------
    def _derive_skin_from_active_vrm(self) -> Optional[str]:
        """Return the skin folder implied by ``self.active_vrm``.

        A string like ``"/skins/Rei/model.vrm"`` yields ``"Rei"``.  Otherwise we
        fall back to ``self._current_skin`` which is explicitly set when a skin
        is activated via the API.  ``None`` is returned for a custom/uploaded
        VRM or when no skin information can be determined.
        """
        if self._current_skin:
            return self._current_skin
        av = getattr(self, "active_vrm", None)
        if isinstance(av, str) and "/skins/" in av:
            parts = av.split("/")
            try:
                idx = parts.index("skins")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
            except ValueError:
                pass
        return None

    async def get_current_skin(self):
        """Handler for **GET /api/skins/current_skin**.

        Returns ``{"skin": <name>}`` where ``<name>`` is the active skin folder or
        ``null`` if the currently selected VRM does not belong to any skin
        (i.e. a custom/uploaded model).  For traditional setups with no custom
        model, ``Rei`` is returned so clients have a sane default.
        """
        skin = self._derive_skin_from_active_vrm()
        if skin is None:
            # Only fall back to Rei when no user-uploaded VRM is currently active.
            # A bare filename (e.g. "model.vrm", no leading "/skins/") means the
            # user has uploaded a custom model — return null in that case.
            av = getattr(self, "active_vrm", None)
            user_vrm_active = (
                isinstance(av, str) and av and not av.startswith("/skins/")
            )
            if not user_vrm_active:
                rei = Path(__file__).parent.parent / "skins" / "Rei"
                if rei.exists():
                    skin = "Rei"
        return JSONResponse({"skin": skin})

    @staticmethod
    def _sanitize_vrm_filename(name: str) -> str:
        stem = Path(name or "avatar").stem
        safe = "".join(ch for ch in stem if ch.isalnum() or ch in ("-", "_")).strip(
            "_-"
        )
        if not safe:
            safe = "avatar"
        return f"{safe}_{uuid.uuid4().hex[:8]}.vrm"

    def _detect_vrm_version(self, vrm_path: Path) -> str:
        """Detect VRM version (0.x or 1.0) by sniffing GLB extensions."""
        try:
            with open(vrm_path, "rb") as f:
                header = f.read(12)
                if header[:4] != b"glTF":
                    return "Unknown"

                # GLB format: header(12), then chunks.
                # First chunk is always JSON.
                chunk_length = int.from_bytes(f.read(4), "little")
                chunk_type = f.read(4)
                if chunk_type != b"JSON":
                    return "Unknown"

                # Read JSON chunk and parse extensions
                import json

                json_data = f.read(chunk_length)
                data = json.loads(json_data.decode("utf-8", errors="ignore"))
                extensions = data.get("extensionsUsed", [])
                if "VRMC_vrm" in extensions:
                    return "1.0"
                if "VRM" in extensions:
                    return "0.x"
                return "Unknown"
        except Exception as e:
            from core.logging_utils import log_debug

            log_debug(f"[webui] VRM version detection failed for {vrm_path}: {e}")
            return "Unknown"

    def _models_payload(self) -> dict:
        models: List[dict] = []
        for path in sorted(self.vrm_dir.glob("*.vrm")):
            try:
                stat = path.stat()
            except OSError:
                continue
            models.append(
                {
                    "name": path.name,
                    "url": f"/avatars/{path.name}",
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                    "active": path.name == self.active_vrm,
                }
            )
        return {"models": models, "active": self.active_vrm}

    async def list_vrm_models(self):
        log_debug(f"{LOG_PREFIX} Listing VRM models from {self.vrm_dir}")
        payload = self._models_payload()
        log_debug(f"{LOG_PREFIX} VRM models payload: {payload}")
        return JSONResponse(payload)

    async def config_summary(self):
        # Export current definitions. If some definitions were populated
        # from hard-coded defaults during import-time (when the event loop
        # was running) attempt a best-effort DB reload so the API returns the
        # authoritative (database) values instead of defaults. This fixes the
        # WebUI showing defaults after a restart when the DB actually has
        # persisted values.
        definitions = config_registry.export_definitions()

        # If any definition appears to be the hard-coded default (and is not
        # env-overridden), trigger a background DB reload to pick up persisted
        # values that were skipped during import-time. This mirrors the
        # behavior in CoreInitializer but makes the `/api/config` endpoint
        # resilient to timing/race conditions.
        try:
            needs_reload = False
            for defn in config_registry._definitions.values():
                try:
                    # Consider it a candidate for reload when the current raw
                    # value equals the serialized default and there's no
                    # env_override (likely loaded at import-time)
                    if (
                        getattr(defn, "loaded", False)
                        and not getattr(defn, "env_override", False)
                        and defn.raw_value is not None
                        and defn.raw_value
                        == config_registry._serialize_value(defn, defn.default)
                    ):
                        needs_reload = True
                        break
                except Exception:
                    continue
            if needs_reload:
                # Attempt to load DB-backed values for all definitions and
                # regenerate the exported list.
                try:
                    await config_registry.load_all_from_db()
                    definitions = config_registry.export_definitions()
                except Exception:
                    # Non-fatal: if DB is not available just continue with
                    # the previously-exported definitions so the endpoint
                    # remains responsive.
                    pass
        except Exception:
            # Defensive: don't allow diagnostic checks to break the API
            pass

        items = []

        # Precompute all actions flagged as unsafe by registered components so
        # AUTONOMY_ALLOWED_ACTIONS can be presented as a choices list.
        unsafe_actions = set()
        try:
            from core.core_initializer import PLUGIN_REGISTRY, INTERFACE_REGISTRY
            from core.cortex_registry import get_cortex_registry

            # Plugins
            for plugin in PLUGIN_REGISTRY.values():
                try:
                    if hasattr(plugin, "get_supported_actions"):
                        sup = plugin.get_supported_actions() or {}
                        for k, v in sup.items():
                            if isinstance(v, dict) and v.get("safe") is False:
                                unsafe_actions.add(k)
                except Exception:
                    pass
            # Interfaces
            for interface in INTERFACE_REGISTRY.values():
                try:
                    if hasattr(interface, "get_supported_actions"):
                        sup = interface.get_supported_actions() or {}
                        for k, v in sup.items():
                            if isinstance(v, dict) and v.get("safe") is False:
                                unsafe_actions.add(k)
                except Exception:
                    pass
            # Cortex engines
            try:
                cortex_reg = get_cortex_registry()
                for name in cortex_reg.get_available_engines():
                    try:
                        inst = cortex_reg.get_engine(name)
                        if inst and hasattr(inst, "get_supported_actions"):
                            sup = inst.get_supported_actions() or {}
                            for k, v in sup.items():
                                if isinstance(v, dict) and v.get("safe") is False:
                                    unsafe_actions.add(k)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            unsafe_actions = set()

        loaded_cortex_engines = set()
        try:
            from core.cortex_registry import get_cortex_registry

            cortex_reg = get_cortex_registry()
            available_cortex_engines = sorted(list(cortex_reg.get_available_engines()))
            for name in cortex_reg.get_available_engines():
                try:
                    if cortex_reg.get_engine(name) is not None:
                        loaded_cortex_engines.add(name)
                except Exception:
                    pass
        except Exception:
            available_cortex_engines = []
            pass

        interface_options = []
        try:
            from core.core_initializer import INTERFACE_REGISTRY

            interface_options = sorted(list(INTERFACE_REGISTRY.keys()))
        except Exception:
            interface_options = []

        component_descriptions = {
            "core": "Core runtime configuration for the Synthetic Heart system.",
            "persona": "Persona identity, triggers, and autonomy preferences.",
            "recon": "Recon preflight controls (language/tone hints and timeouts).",
            "debrief": "Debrief postflight recovery and audit behavior.",
            "grillo": "Grillo scheduling and internal beat behavior.",
            "grillo_chat_observer": "Grillo chat observer scheduling and sampling.",
            "history_engine": "History context assembly and trimming controls.",
            "logchat": "LogChat notification and logging thresholds.",
            "logging": "General logging verbosity and output controls.",
            "message_chain": "Message handling and outbound action detection rules.",
            "message_send": "Outbound message delivery tuning.",
            "action_safety": "Action execution safety policy settings.",
            "weather_plugin": "Weather plugin scheduling and delivery settings.",
            "cortex": "Cortex engine selection for base, trainer, and Grillo scopes.",
        }

        try:
            from core.plugin_instance import plugin as active_plugin

            if active_plugin is not None:
                module_name = active_plugin.__class__.__module__.split(".")[-1]
                loaded_cortex_engines.add(module_name)
        except Exception:
            pass

        for entry in definitions:
            # Skip bootstrap-tagged items (not meant for UI)
            if "bootstrap" in entry.get("tags", []):
                continue

            # Skip hidden entries (API-only)
            if entry.get("hidden"):
                continue

            # Hide Cortex-engine-specific variables unless their engine is registered
            if (
                "cortex_engine" in entry.get("tags", [])
                and entry.get("component") not in loaded_cortex_engines
                and entry.get("component") not in available_cortex_engines
            ):
                continue
            component_label = self._get_display_name(entry["component"], None)

            # Get exposed variable definition to extract ui_type and options
            from core.variables_engine import exposed_vars

            exposed_def = exposed_vars.get_definition(entry["key"])
            ui_type = (
                exposed_def.ui_type if exposed_def else entry.get("ui_type", "string")
            )
            options = exposed_def.options if exposed_def else []

            # If no explicit options from exposed_vars, try deriving from constraints
            if not options and entry.get("constraints"):
                constraints = entry["constraints"]
                if isinstance(constraints, dict) and "choices" in constraints:
                    choices = constraints["choices"]
                    if choices and len(choices) > 0:
                        options = choices
                        ui_type = "combobox"

            # If this is the autonomy whitelist, present unsafe actions as choices
            if entry.get("key") == "AUTONOMY_ALLOWED_ACTIONS":
                options = sorted(list(unsafe_actions))
                log_info(
                    f"{LOG_PREFIX} AUTONOMY_ALLOWED_ACTIONS options populated: {len(options)} actions found. {options}"
                )

            # Cortex selection dropdowns (registered cortex engines)
            if entry.get("key") in (
                "BASE_CORTEX",
                "GRILLO_CORTEX",
                "TRAINER_CORTEX",
                "LIVE_CORTEX",
            ):
                ui_type = "select"
                if entry.get("key") == "LIVE_CORTEX":
                    from core.cortex_registry import get_cortex_registry as _get_cr

                    # include any engines registered in the Live registry as well
                    from core.live_registry import LIVE_REGISTRY

                    live_engines = _get_cr().get_engines_by_cortex("live")
                    extra = LIVE_REGISTRY.get_available_engines()
                    combined = sorted(set(live_engines + extra))
                    # always allow explicit disable
                    options = ["Default", "disabled"] + combined
                elif entry.get("key") in ("GRILLO_CORTEX", "TRAINER_CORTEX"):
                    options = ["Default"] + available_cortex_engines
                else:
                    options = available_cortex_engines
                if not options and entry.get("value"):
                    options = [str(entry.get("value"))]

            # Trainer IDs: render a structured list with interface dropdown
            if entry.get("key") == "TRAINER_IDS":
                ui_type = "trainer-ids"
                options = interface_options

            # Derive optional subgroup labels for nicer grouping in the UI (e.g. GRILLO_DREAM_* → 'Grillo Dream')
            subgroup = None
            key = entry["key"]
            # Simple prefix-based mapping for known plugin subgroups
            subgroup_map = {
                "GRILLO_DREAM_": "Grillo Dream",
                "GRILLO_OBSERVER_": "Grillo Observer",
                "HISTORY_EVALUATOR_": "History Evaluator",
                "GRILLO_": "Grillo",
                "LIVE_": "Live",
                "SYNTH_": "Persona",
            }
            for prefix, label in subgroup_map.items():
                if key.startswith(prefix):
                    subgroup = label
                    break

            items.append(
                {
                    "key": entry["key"],
                    "label": entry["label"],
                    "description": entry["description"],
                    "value": entry["value"],
                    "default": entry["default"],
                    "group": entry["group"],
                    "component": entry["component"],
                    "component_label": component_label,
                    "component_description": component_descriptions.get(
                        entry["component"], f"Settings for {component_label}."
                    ),
                    "subgroup": subgroup,
                    "advanced": entry["advanced"],
                    "sensitive": entry["sensitive"],
                    "env_override": entry["env_override"],
                    "value_type": entry["value_type"],
                    # A variable is editable only if it's not overridden by env AND not explicitly readonly
                    "editable": (not entry["env_override"])
                    and (not entry.get("readonly", False)),
                    "constraints": entry.get("constraints"),
                    "ui_type": ui_type,
                    "options": options,
                }
            )

        return JSONResponse(
            {
                "items": items,
                "messages": {
                    "env_override": "Variables marked with ⚠️ icon are overridden by environment values. Remove the override to re-enable editing.",
                    "advanced_warning": "Changing network ports may render the service unavailable. Update Docker compose exposure before applying.",
                },
            }
        )

    async def get_animations_for_type(self, skin: str, animation_type: str):
        """Return list of animation files for a specific skin and animation type.

        Example: GET /api/animations/Rei/idle
        Returns: {"animations": ["Idle.fbx", "Idle2.fbx", "Look Around.fbx"]}
        """
        try:
            # Validate skin and animation_type to prevent directory traversal
            if ".." in skin or ".." in animation_type:
                raise HTTPException(
                    status_code=400, detail="Invalid skin or animation type"
                )

            anim_dir = (
                Path(__file__).parent.parent
                / "skins"
                / skin
                / "animations"
                / animation_type
            )

            if not anim_dir.exists():
                log_debug(f"{LOG_PREFIX} Animation directory not found: {anim_dir}")
                return JSONResponse({"animations": []})

            # Get all .fbx files in the directory (non-recursive, ignore subdirectories)
            fbx_files = sorted(
                [
                    f.name
                    for f in anim_dir.iterdir()
                    if f.is_file() and f.suffix.lower() == ".fbx"
                ]
            )

            log_debug(
                f"{LOG_PREFIX} Found {len(fbx_files)} animations in {skin}/{animation_type}: {fbx_files}"
            )
            return JSONResponse({"animations": fbx_files})
        except Exception as e:
            log_error(
                f"{LOG_PREFIX} Error listing animations for {skin}/{animation_type}: {e}"
            )
            return JSONResponse({"animations": []}, status_code=500)

    async def get_animation_descriptor(
        self, skin: str, animation_type: str, animation_file: str
    ):
        """Return the animation descriptor JSON for a given animation.

        This endpoint will return the on-disk descriptor if present (e.g.,
        ``<animation>.fbx.json``). If no descriptor file exists the endpoint
        returns an implicit descriptor with sensible defaults (see AGENTS.md):
        - `idle` animations default to looping (``play_once=false``)
        - non-idle animations default to playing once (``play_once=true``)
        """
        try:
            # Basic validation to prevent directory traversal
            if ".." in skin or ".." in animation_type or ".." in animation_file:
                raise HTTPException(
                    status_code=400, detail="Invalid skin or animation type"
                )

            anim_dir = (
                Path(__file__).parent.parent
                / "skins"
                / skin
                / "animations"
                / animation_type
            )
            if not anim_dir.exists() or not anim_dir.is_dir():
                log_debug(
                    f"{LOG_PREFIX} Animation directory not found for descriptor request: {anim_dir}"
                )
                raise HTTPException(status_code=404, detail="Animation type not found")

            # Descriptor file is expected to be the animation filename + '.json'
            # e.g., 'Idle2.fbx.json' or 'Look Around.fbx.json'. Try the JSON
            # descriptor first (safe), and only fall back to implicit defaults
            # if the JSON descriptor is missing. Avoid opening the raw .fbx binary
            # which would cause JSON parsing errors and 500 responses.
            # Handle cases where the incoming `animation_file` already includes
            # the '.json' suffix just in case.
            base_name = (
                animation_file[:-5]
                if animation_file.endswith(".json")
                else animation_file
            )
            desc_json_path = anim_dir / (base_name + ".json")

            log_debug(f"{LOG_PREFIX} Resolving descriptor: json={desc_json_path}")

            if desc_json_path.exists() and desc_json_path.is_file():
                try:
                    content = desc_json_path.read_text(encoding="utf-8")
                    try:
                        return JSONResponse(json.loads(content))
                    except Exception as exc:  # pragma: no cover - defensive
                        # JSON malformed: log and fall through to implicit descriptor
                        log_warning(
                            f"{LOG_PREFIX} Failed to parse descriptor JSON {desc_json_path}: {exc}"
                        )
                except Exception as exc:  # pragma: no cover - defensive
                    # I/O error reading descriptor: log and fall through to implicit descriptor
                    log_warning(
                        f"{LOG_PREFIX} Failed to read descriptor {desc_json_path}: {exc}"
                    )

            # No descriptor file found or descriptor unreadable: return an implicit descriptor based
            # on animation_type (idle -> loop by default; others play_once).
            play_once_default = False if animation_type.lower() == "idle" else True
            implicit = {"play_once": play_once_default}
            return JSONResponse(implicit)
        except HTTPException:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log_error(
                f"{LOG_PREFIX} Error resolving animation descriptor for {skin}/{animation_type}/{animation_file}: {exc}"
            )
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def service_worker(self) -> FileResponse:
        """Serve the WebUI service worker script at the root path."""
        sw_path = (
            Path(__file__).parent.parent
            / "res"
            / "synth_webui"
            / "static"
            / "service-worker.js"
        )
        if not sw_path.exists() or not sw_path.is_file():
            raise HTTPException(status_code=404, detail="Service worker not found")
        return FileResponse(sw_path, media_type="application/javascript")

    async def favicon(self) -> FileResponse:
        """Serve a bundled favicon to avoid 404s on /favicon.ico."""
        icon_path = (
            Path(__file__).parent.parent
            / "res"
            / "synth_webui"
            / "static"
            / "synth_icon_192.png"
        )
        if not icon_path.exists() or not icon_path.is_file():
            raise HTTPException(status_code=404, detail="Favicon not found")
        return FileResponse(icon_path, media_type="image/png")

    async def upload_animation(
        self,
        file: UploadFile = File(...),
        state: str = Form(...),
        descriptor: Optional[str] = Form(None),
        tags: Optional[str] = Form(None),
        upload_id: Optional[str] = Form(None),
    ):
        """Upload a temporary animation file and register it for playback."""
        if not file or not file.filename:
            raise HTTPException(status_code=400, detail="No file uploaded")

        filename = animation_uploads.sanitize_filename(file.filename)
        ext = Path(filename).suffix.lower()
        if ext not in {".fbx", ".vrma"}:
            raise HTTPException(
                status_code=400, detail="Only .fbx or .vrma files are accepted"
            )

        try:
            upload_id = animation_uploads.sanitize_upload_id(upload_id)
            normalized_state = animation_uploads.normalize_state(state)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        target_dir = animation_uploads.get_state_dir(upload_id, normalized_state)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filename

        bytes_written = 0
        try:
            with target_path.open("wb") as buffer:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    buffer.write(chunk)
                    bytes_written += len(chunk)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to store animation upload: {exc}")
            if target_path.exists():
                try:
                    target_path.unlink()
                except Exception:
                    pass
            raise HTTPException(
                status_code=500, detail="Failed to store animation file"
            ) from exc
        finally:
            await file.close()

        descriptor_path = None
        if descriptor:
            try:
                descriptor_payload = json.loads(descriptor)
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail="descriptor must be valid JSON"
                ) from exc

            descriptor_path = target_path.with_suffix(target_path.suffix + ".json")
            try:
                with descriptor_path.open("w", encoding="utf-8") as handle:
                    json.dump(descriptor_payload, handle, indent=2, sort_keys=True)
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Failed to store descriptor: {exc}")

        tag_list: Optional[List[str]] = None
        if tags:
            try:
                parsed = json.loads(tags)
                if isinstance(parsed, list):
                    tag_list = [str(tag) for tag in parsed]
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail="tags must be a JSON array"
                ) from exc

        meta = animation_uploads.record_upload(
            upload_id,
            normalized_state,
            filename,
            size_bytes=bytes_written,
            tags=tag_list,
            descriptor_path=descriptor_path,
            original_filename=file.filename,
        )

        try:
            animations_root = (
                animation_uploads.get_upload_root(upload_id) / "animations"
            )
            if getattr(self, "animation_handler", None):
                self.animation_handler.add_temporary_search_path(animations_root)
                self.animation_handler.register_temporary_state_override(
                    upload_id=upload_id,
                    state=normalized_state,
                    animations=[filename],
                )
        except Exception as exc:
            log_warning(
                f"{LOG_PREFIX} Failed to register temporary animation search path: {exc}"
            )

        url = f"/skins/temp/{upload_id}/animations/{normalized_state}/{filename}"
        return JSONResponse(
            {
                "status": "ok",
                "upload_id": upload_id,
                "state": normalized_state,
                "filename": filename,
                "url": url,
                "meta": meta,
            },
            status_code=201,
        )

    async def list_animation_uploads(self):
        """List temporary animation uploads and their metadata."""
        uploads = animation_uploads.list_uploads()
        payload: List[Dict[str, Any]] = []
        for meta in uploads:
            upload_id = meta.get("upload_id")
            states = meta.get("states", {})
            for state, files in states.items():
                payload.append(
                    {
                        "upload_id": upload_id,
                        "state": state,
                        "files": files,
                        "url_prefix": f"/skins/temp/{upload_id}/animations/{state}",
                        "created_at": meta.get("created_at"),
                        "tags": meta.get("tags", []),
                    }
                )
        return JSONResponse({"uploads": payload})

    async def delete_animation_upload(self, upload_id: str):
        """Delete a temporary animation upload and deregister it."""
        removed = animation_uploads.delete_upload(upload_id)
        try:
            animations_root = (
                animation_uploads.get_upload_root(upload_id) / "animations"
            )
            if getattr(self, "animation_handler", None):
                self.animation_handler.remove_temporary_search_path(animations_root)
        except Exception:
            pass
        if not removed:
            raise HTTPException(status_code=404, detail="Upload not found")
        return JSONResponse({"status": "ok", "removed": upload_id})

    async def promote_animation_upload(self, request: Request):
        """Promote a temporary upload into a permanent skin animation folder."""
        if os.getenv("SYNTH_MATEENGINE_PROMOTE_ENABLED", "0") != "1":
            raise HTTPException(
                status_code=403, detail="Promotion is disabled on this server"
            )
        data = await request.json()
        upload_id = data.get("upload_id")
        target_skin = data.get("target_skin")
        if not upload_id or not target_skin:
            raise HTTPException(
                status_code=400, detail="upload_id and target_skin are required"
            )

        target_state = data.get("target_state")
        overwrite = bool(data.get("overwrite", False))
        rename = data.get("rename")

        try:
            promoted = animation_uploads.promote_upload(
                upload_id,
                target_skin=target_skin,
                target_state=target_state,
                overwrite=overwrite,
                rename=rename,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        promoted_urls = []
        for path in promoted:
            try:
                rel = path.relative_to(Path(__file__).resolve().parent.parent)
                promoted_urls.append(f"/{rel.as_posix()}")
            except Exception:
                promoted_urls.append(str(path))

        return JSONResponse(
            {"status": "ok", "promoted": promoted_urls, "skin": target_skin}
        )

    async def get_prompt_override(self, request: Request):
        """Return prompt override/injection instructions for external interfaces."""
        override = os.getenv("SYNTH_MATEENGINE_PROMPT_OVERRIDE", "0") == "1"
        host = None
        try:
            if request and request.client:
                host = request.client.host
        except Exception:
            host = None

        os_name = os.getenv("SYNTH_HOST_OS") or platform.platform()
        host_label = host or "unknown-host"

        injection = (
            "Interface note (MateEngine, host={host}, os={os}): "
            "This interface may request limited animation control and UI hints; "
            "do not replace SyntH system instructions."
        ).format(host=host_label, os=os_name)

        payload = {
            "override": override,
            "injection": injection,
            "source": "MateEngine",
            "static": True,
            "metadata": {
                "os": os_name,
                "host": host_label,
                "controls": {"animations": "limited"},
            },
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        return JSONResponse(payload)

    async def enqueue_outbox(
        self, source: str, *, text: str, target: Optional[str] = None
    ) -> None:
        """Enqueue a message for a named integration outbox (public API).

        Args:
            source: integration source name (e.g., 'mate')
            text: message text
            target: optional recipient target
        """
        # Ensure the destination outbox exists
        self.ensure_integration_outbox(source)
        entry = {
            "text": text,
            "target": target,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        async with self._integration_outbox_locks[source]:
            self._integration_outboxes[source].append(entry)

    # Backwards compatibility shim (internal use only)
    async def _enqueue_integration_outbox(
        self, *, source: str, text: str, target: Optional[str] = None
    ) -> None:
        await self.enqueue_outbox(source, text=text, target=target)

    async def post_integration_message(self, request: Request):
        """Accept messages from external integrations (generic). Expected JSON:
        { "source": "mate", "type": "chat", "payload": {...}, "metadata": {...} }
        """
        data = await request.json()
        source = data.get("source")
        typ = data.get("type") or data.get("message_type") or "chat"
        payload = data.get("payload") or {}
        metadata = data.get("metadata") or {}
        if not source:
            raise HTTPException(status_code=400, detail="'source' is required")
        if typ in ("chat", "message"):
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise HTTPException(
                    status_code=400,
                    detail="'payload.text' must be a non-empty string for chat messages",
                )
            # Create a lightweight message object for routing
            from types import SimpleNamespace

            msg = SimpleNamespace()
            msg.chat_id = payload.get("conversation_id") or None
            msg.interface_path = f"integration:{source}"
            # Forward into the message queue so plugins and the core can handle it
            from core import message_queue

            result = await message_queue.enqueue_and_wait(
                bot=None,
                message=msg,
                context_memory={"integration_source": source, "metadata": metadata},
                history_scope="local",
                priority=False,
                interface_id="integration",
                skip_mention_check=True,
                original_message=msg,
                timeout=30.0,
            )
            return JSONResponse({"status": "ok", "result": result})
        else:
            # Other message types: store in integration outbox
            text = payload.get("text") or json.dumps(payload)
            await self._enqueue_integration_outbox(
                source=source, text=text, target=payload.get("target")
            )
            return JSONResponse({"status": "ok", "stored": True})

    async def get_integration_outbox(self, request: Request):
        """Return and clear queued messages for a given integration source."""
        params = request.query_params
        source = params.get("source")
        if not source:
            raise HTTPException(
                status_code=400, detail="'source' query param is required"
            )
        self.ensure_integration_outbox(source)
        async with self._integration_outbox_locks[source]:
            items = list(self._integration_outboxes[source])
            self._integration_outboxes[source].clear()
        return JSONResponse({"messages": items})

    def ensure_integration_outbox(self, src: str) -> None:
        """Create per-source outbox and lock if they don't exist."""
        if src not in self._integration_outboxes:
            self._integration_outboxes[src] = deque(maxlen=500)
            import asyncio as _asyncio

            self._integration_outbox_locks[src] = _asyncio.Lock()

    # ---------------------------------------------------------------------------
    # External endpoints (custom AI service connections)
    # ---------------------------------------------------------------------------

    async def _run_auto_probe(
        self,
        ep_id: int,
        api_key: str,
        reg: "ExternalEndpointRegistry",  # type: ignore[name-defined]  # noqa: F821
    ) -> dict:
        """Run probe_endpoint with a timeout and save the result.

        Never raises — errors are captured and returned as a failed probe dict.
        """
        import asyncio
        import os

        try:
            from core.external_endpoints.probe import probe_endpoint

            ep = await reg.get_endpoint(ep_id)
            if ep is None:
                return {"status": "failed", "error": "Endpoint not found"}

            timeout_seconds = float(
                os.getenv(
                    "EXTERNAL_ENDPOINT_PROBE_TIMEOUT_SECONDS",
                    str(EXTERNAL_ENDPOINT_PROBE_TIMEOUT_SECONDS),
                )
            )
            result = await asyncio.wait_for(
                probe_endpoint(ep, api_key), timeout=timeout_seconds
            )
            await reg.set_probe_result(
                ep_id,
                status=result.status,
                capabilities=result.capabilities,
                models=result.models,
                models_metadata=result.models_metadata,
            )
            return {
                "status": result.status,
                "capabilities": result.capabilities,
                "models": result.models,
                "ping_echo": result.ping_echo,
                "error": result.error_message,
            }
        except asyncio.TimeoutError:
            log_warning(f"{LOG_PREFIX} auto-probe timed out for ep_id={ep_id}")
            return {
                "status": "failed",
                "error": f"Probe timed out ({os.getenv('EXTERNAL_ENDPOINT_PROBE_TIMEOUT_SECONDS', str(EXTERNAL_ENDPOINT_PROBE_TIMEOUT_SECONDS))} s)",
            }
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} auto-probe failed for ep_id={ep_id}: {exc}")
            return {"status": "failed", "error": str(exc)}

    async def list_external_endpoints(self) -> JSONResponse:
        """GET /api/external-endpoints — list all configured external endpoints."""
        try:
            from core.external_endpoints.registry import get_external_endpoint_registry

            endpoints = await get_external_endpoint_registry().list_endpoints()
            return JSONResponse({"endpoints": [ep.to_dict() for ep in endpoints]})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} list_external_endpoints failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def list_external_endpoint_presets(self) -> JSONResponse:
        """GET /api/external-endpoints/presets — list available provider presets.

        Reads JSON files from the project-level ``providers/`` directory.  Each
        file describes a known AI provider (Gemini, Anthropic, OpenRouter, …)
        with default values that the UI wizard can use to pre-fill the add form.
        Files can be deleted by the user without breaking the rest of the system.
        """
        try:
            from core.external_endpoints.preset_registry import load_presets

            presets = load_presets()
            return JSONResponse({"presets": presets})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} list_external_endpoint_presets failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def create_external_endpoint(self, request: Request) -> JSONResponse:
        """POST /api/external-endpoints — add a new external endpoint."""
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        name = str(data.get("name") or "").strip()
        base_url = str(data.get("base_url") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Missing 'name'")
        if not base_url:
            raise HTTPException(status_code=400, detail="Missing 'base_url'")

        api_key_str = str(data.get("api_key") or "")
        # Extract subsystem_map if provided by the form (capabilities section)
        raw_smap = data.get("subsystem_map")
        subsystem_map: dict[str, bool] | None = None
        if isinstance(raw_smap, dict):
            subsystem_map = {k: bool(v) for k, v in raw_smap.items()}
        try:
            from core.external_endpoints.registry import get_external_endpoint_registry

            reg = get_external_endpoint_registry()
            ep = await reg.add_endpoint(
                name=name,
                base_url=base_url,
                protocol=str(data.get("protocol") or "openai"),
                api_key=api_key_str,
                display_label=str(data.get("display_label") or ""),
                extra_config=data.get("extra_config"),
                subsystem_map=subsystem_map,
            )

            # Auto-probe immediately after creation
            probe_data = await self._run_auto_probe(ep.id, api_key_str, reg)

            # Reload endpoint so probe fields are reflected
            ep_updated = await reg.get_endpoint(ep.id) or ep
            return JSONResponse(
                {"endpoint": ep_updated.to_dict(), "probe": probe_data},
                status_code=201,
            )
        except Exception as exc:
            log_error(f"{LOG_PREFIX} create_external_endpoint failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def get_external_endpoint(self, ep_id: int) -> JSONResponse:
        """GET /api/external-endpoints/{ep_id}."""
        try:
            from core.external_endpoints.registry import get_external_endpoint_registry

            ep = await get_external_endpoint_registry().get_endpoint(ep_id)
            if ep is None:
                raise HTTPException(status_code=404, detail="Endpoint not found")
            return JSONResponse({"endpoint": ep.to_dict()})
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} get_external_endpoint failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def update_external_endpoint(
        self, ep_id: int, request: Request
    ) -> JSONResponse:
        """PUT /api/external-endpoints/{ep_id} — update fields."""
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        try:
            from core.external_endpoints.crypto import decrypt_api_key
            from core.external_endpoints.registry import get_external_endpoint_registry

            reg = get_external_endpoint_registry()
            ep = await reg.update_endpoint(ep_id, **data)
            if ep is None:
                raise HTTPException(status_code=404, detail="Endpoint not found")

            # Auto-probe on every save (including label-only changes)
            api_key = decrypt_api_key(ep.api_key_enc or "")
            probe_data = await self._run_auto_probe(ep.id, api_key, reg)

            # Reload endpoint so probe fields are reflected
            ep_updated = await reg.get_endpoint(ep.id) or ep
            return JSONResponse({"endpoint": ep_updated.to_dict(), "probe": probe_data})
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} update_external_endpoint failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def delete_external_endpoint(self, ep_id: int) -> JSONResponse:
        """DELETE /api/external-endpoints/{ep_id}."""
        try:
            from core.external_endpoints.registry import get_external_endpoint_registry

            removed = await get_external_endpoint_registry().remove_endpoint(ep_id)
            if not removed:
                raise HTTPException(status_code=404, detail="Endpoint not found")
            return JSONResponse({"status": "deleted"})
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} delete_external_endpoint failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def probe_external_endpoint(self, ep_id: int) -> JSONResponse:
        """POST /api/external-endpoints/{ep_id}/probe — probe capabilities."""
        try:
            from core.external_endpoints.crypto import decrypt_api_key
            from core.external_endpoints.registry import get_external_endpoint_registry

            reg = get_external_endpoint_registry()
            ep = await reg.get_endpoint(ep_id)
            if ep is None:
                raise HTTPException(status_code=404, detail="Endpoint not found")

            api_key = decrypt_api_key(ep.api_key_enc or "")
            probe_data = await self._run_auto_probe(ep_id, api_key, reg)
            return JSONResponse(
                {
                    "status": probe_data.get("status", "failed"),
                    "capabilities": probe_data.get("capabilities", {}),
                    "models": probe_data.get("models", []),
                    "error": probe_data.get("error", ""),
                }
            )
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} probe_external_endpoint failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def ping_external_endpoint(
        self, ep_id: int, request: Request
    ) -> JSONResponse:
        """POST /api/external-endpoints/{ep_id}/ping — ping with a specific model."""
        try:
            from core.external_endpoints.crypto import decrypt_api_key
            from core.external_endpoints.probe import get_adapter_for_endpoint
            from core.external_endpoints.registry import get_external_endpoint_registry

            reg = get_external_endpoint_registry()
            ep = await reg.get_endpoint(ep_id)
            if ep is None:
                raise HTTPException(status_code=404, detail="Endpoint not found")

            body: dict = {}
            try:
                body = await request.json()
            except Exception:
                pass
            model: str | None = body.get("model") or None

            api_key = decrypt_api_key(ep.api_key_enc or "")
            adapter = get_adapter_for_endpoint(ep, api_key)
            ok, echo = await adapter.ping_test(
                model=model,
                timeout=EXTERNAL_ENDPOINT_PROBE_TIMEOUT_SECONDS,
            )
            return JSONResponse(
                {
                    "ok": ok,
                    "echo": echo,
                    "model": model or ep.default_model or "",
                }
            )
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} ping_external_endpoint failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def enable_external_endpoint(self, ep_id: int) -> JSONResponse:
        """POST /api/external-endpoints/{ep_id}/enable."""
        try:
            from core.external_endpoints.registry import get_external_endpoint_registry

            reg = get_external_endpoint_registry()
            ep = await reg.update_endpoint(ep_id, enabled=True)
            if ep is None:
                raise HTTPException(status_code=404, detail="Endpoint not found")
            return JSONResponse({"endpoint": ep.to_dict()})
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} enable_external_endpoint failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def disable_external_endpoint(self, ep_id: int) -> JSONResponse:
        """POST /api/external-endpoints/{ep_id}/disable."""
        try:
            from core.external_endpoints.registry import get_external_endpoint_registry

            reg = get_external_endpoint_registry()
            ep = await reg.update_endpoint(ep_id, enabled=False)
            if ep is None:
                raise HTTPException(status_code=404, detail="Endpoint not found")
            return JSONResponse({"endpoint": ep.to_dict()})
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} disable_external_endpoint failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def set_external_endpoint_mapping(
        self, ep_id: int, request: Request
    ) -> JSONResponse:
        """PUT /api/external-endpoints/{ep_id}/mapping — set subsystem mapping overrides."""
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")

        try:
            from core.external_endpoints.registry import get_external_endpoint_registry

            reg = get_external_endpoint_registry()
            ep = await reg.get_endpoint(ep_id)
            if ep is None:
                raise HTTPException(status_code=404, detail="Endpoint not found")
            await reg.set_subsystem_map(ep_id, {k: bool(v) for k, v in data.items()})
            ep = await reg.get_endpoint(ep_id)
            return JSONResponse({"endpoint": ep.to_dict() if ep else {}})
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} set_external_endpoint_mapping failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def set_external_endpoint_model(
        self, ep_id: int, request: Request
    ) -> JSONResponse:
        """PUT /api/external-endpoints/{ep_id}/model — set default model."""
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        model = str(data.get("model") or "").strip()

        try:
            from core.external_endpoints.registry import get_external_endpoint_registry

            reg = get_external_endpoint_registry()
            ep = await reg.get_endpoint(ep_id)
            if ep is None:
                raise HTTPException(status_code=404, detail="Endpoint not found")
            await reg.set_default_model(ep_id, model)
            ep = await reg.get_endpoint(ep_id)
            return JSONResponse({"endpoint": ep.to_dict() if ep else {}})
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} set_external_endpoint_model failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def diary_summary(self, request: Request):
        """Return persona snapshot and recent diary entries for the Diary tab."""
        params = request.query_params

        def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            return max(minimum, min(maximum, parsed))

        days = _bounded_int(params.get("days"), default=14, minimum=1, maximum=365)
        limit = _bounded_int(params.get("limit"), default=100, minimum=1, maximum=1000)
        max_chars_param = params.get("max_chars")
        if max_chars_param is not None:
            max_chars = _bounded_int(
                max_chars_param, default=20000, minimum=1000, maximum=200000
            )
        else:
            max_chars = 20000
        include_archived = params.get("include_archived", "false").lower() == "true"

        # Pagination parameters
        page = _bounded_int(params.get("page"), default=1, minimum=1, maximum=1000)
        per_page = _bounded_int(
            params.get("per_page"), default=10, minimum=1, maximum=1000
        )
        search = params.get("search", "").strip()

        persona_snapshot = await self._fetch_persona_snapshot()
        diary_payload = await self._fetch_diary_entries(
            days=days,
            limit=limit,
            max_chars=max_chars,
            include_archived=include_archived,
            page=page,
            per_page=per_page,
            search=search,
        )

        if not persona_snapshot.get("created_at") and diary_payload.get(
            "earliest_timestamp"
        ):
            persona_snapshot["created_at"] = diary_payload["earliest_timestamp"]

        response = {
            "persona": persona_snapshot,
            "diary": {
                "available": diary_payload["available"],
                "plugin_enabled": diary_payload["plugin_enabled"],
                "entries": diary_payload["entries"],
                "count": diary_payload["count"],
                "total_count": diary_payload["total_count"],
                "page": page,
                "per_page": per_page,
                "total_pages": diary_payload["total_pages"],
                "days": days,
                "limit": limit,
                "max_chars": max_chars,
                "include_archived": include_archived,
                "earliest_timestamp": diary_payload["earliest_timestamp"],
                "latest_timestamp": diary_payload["latest_timestamp"],
                "error": diary_payload.get("error"),
            },
        }
        # Clean ConfigVar proxies from response before JSON serialization
        response = self._clean_for_json(response)
        return JSONResponse(response)

    def _clean_for_json(self, obj: Any) -> Any:
        """Recursively convert ConfigVar proxies and other non-JSON-serializable objects to JSON-safe types."""
        if isinstance(obj, dict):
            return {k: self._clean_for_json(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._clean_for_json(item) for item in obj]
        elif hasattr(obj, "__class__") and obj.__class__.__name__ == "ConfigVar":
            # ConfigVar proxy - convert to string
            return str(obj)
        elif isinstance(obj, (str, int, float, bool, type(None))):
            # Already JSON-serializable
            return obj
        else:
            # Fallback for unknown types
            return str(obj)

    async def _fetch_persona_snapshot(self) -> Dict[str, Any]:
        """Load core persona information for display."""
        snapshot: Dict[str, Any] = {
            "available": False,
            "id": None,
            "name": None,
            "aliases": [],
            "profile": None,
            "created_at": None,
            "last_updated": None,
            "emotive_state": [],
            "dominant_emotion": None,
        }

        try:
            from core.persona_manager import (  # type: ignore
                get_persona_manager,
            )
        except Exception as exc:  # pragma: no cover - defensive import
            log_debug(f"{LOG_PREFIX} Persona manager unavailable: {exc}")
            snapshot["error"] = str(exc)
            return snapshot

        persona = None
        try:
            manager = get_persona_manager()
            if manager:
                persona = manager.get_current_persona()
                if persona is None and hasattr(manager, "async_init"):
                    try:
                        await manager.async_init()
                        persona = manager.get_current_persona()
                    except Exception as async_exc:
                        log_debug(
                            f"{LOG_PREFIX} Persona async_init failed: {async_exc}"
                        )
                if persona is None:
                    persona = await manager.load_persona("default")
                    if persona is not None:
                        try:
                            manager._current_persona = persona  # type: ignore[attr-defined]
                            manager._persona_loaded = True  # type: ignore[attr-defined]
                        except Exception:
                            pass
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Unable to load persona: {exc}")

        if not persona:
            # Fallback: try to populate snapshot from exposed config values
            try:
                from core.config_manager import config_registry

                name = config_registry.get_value("SYNTH_NAME", None)
                aliases_raw = config_registry.get_value(
                    "SYNTH_ALIASES", None, value_type="json"
                )
                profile = config_registry.get_value("SYNTH_PROFILE", None)

                # Convert ConfigVar proxies to actual values
                if hasattr(name, "__str__"):
                    name = str(name) if name else None
                if hasattr(profile, "__str__"):
                    profile = str(profile) if profile else None

                aliases = []
                if aliases_raw:
                    # Convert ConfigVar if needed
                    if hasattr(aliases_raw, "__str__"):
                        aliases_raw = str(aliases_raw)

                    # aliases_raw may be a JSON string or a list
                    if isinstance(aliases_raw, str):
                        import json

                        try:
                            parsed = json.loads(aliases_raw)
                            if isinstance(parsed, list):
                                aliases = parsed
                        except Exception:
                            # not JSON - try splitting
                            aliases = [
                                a.strip() for a in aliases_raw.split(",") if a.strip()
                            ]
                    elif isinstance(aliases_raw, list):
                        aliases = aliases_raw

                if name or aliases or profile:
                    snapshot.update(
                        {
                            "available": True,
                            "id": "default",
                            "name": name or None,
                            "aliases": aliases,
                            "profile": profile or None,
                        }
                    )
                    return snapshot
            except Exception as exc:
                log_debug(f"{LOG_PREFIX} Persona fallback from config failed: {exc}")

            return snapshot

        def _format_emotions(emotions: Optional[List[Any]]) -> List[Dict[str, Any]]:
            formatted: List[Dict[str, Any]] = []
            if not emotions:
                return formatted
            for state in emotions:
                if isinstance(state, dict):
                    emotion_type = str(state.get("type") or "").strip().lower()
                    intensity = float(state.get("intensity", 0))
                elif hasattr(state, "type") and hasattr(state, "intensity"):
                    emotion_type = str(state.type).strip().lower()
                    intensity = float(state.intensity)
                else:
                    continue
                if not emotion_type:
                    continue
                formatted.append(
                    {
                        "type": emotion_type,
                        "intensity": max(0.0, min(10.0, intensity)),
                    }
                )
            formatted.sort(key=lambda item: item["intensity"], reverse=True)
            return formatted

        emotions = _format_emotions(getattr(persona, "emotive_state", []))
        dominant = emotions[0] if emotions else None

        snapshot.update(
            {
                "available": True,
                "id": getattr(persona, "id", None),
                "name": getattr(persona, "name", None) or None,
                "aliases": getattr(persona, "aliases", []) or [],
                "profile": getattr(persona, "profile", None) or None,
                "created_at": getattr(persona, "created_at", None) or None,
                "last_updated": getattr(persona, "last_updated", None) or None,
                "emotive_state": emotions,
                "dominant_emotion": dominant,
            }
        )
        return snapshot

    async def _fetch_diary_entries(
        self,
        *,
        days: int,
        limit: int,
        max_chars: int,
        include_archived: bool = False,
        page: int = 1,
        per_page: int = 10,
        search: str = "",
    ) -> Dict[str, Any]:
        """Retrieve diary entries via the AI diary plugin when available."""
        payload: Dict[str, Any] = {
            "available": False,
            "plugin_enabled": False,
            "entries": [],
            "count": 0,
            "total_count": 0,
            "total_pages": 0,
            "earliest_timestamp": None,
            "latest_timestamp": None,
        }

        try:
            from plugins import ai_diary  # type: ignore
        except Exception as exc:  # pragma: no cover - defensive import
            log_debug(f"{LOG_PREFIX} Diary plugin unavailable: {exc}")
            payload["error"] = str(exc)
            return payload

        plugin_enabled = bool(getattr(ai_diary, "PLUGIN_ENABLED", True))
        payload["plugin_enabled"] = plugin_enabled

        if not plugin_enabled:
            payload["error"] = "Diary plugin disabled"
            return payload

        try:
            # Get total count first
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    if include_archived:
                        await cur.execute("SELECT COUNT(*) FROM ai_diary")
                        diary_count = (await cur.fetchone())[0]
                        await cur.execute("SELECT COUNT(*) FROM ai_diary_archive")
                        archive_count = (await cur.fetchone())[0]
                        total_count = diary_count + archive_count
                    else:
                        await cur.execute("SELECT COUNT(*) FROM ai_diary")
                        result = await cur.fetchone()
                        total_count = result[0] if result else 0

            payload["total_count"] = total_count
            payload["total_pages"] = (
                (total_count + per_page - 1) // per_page
                if per_page != "unlimited"
                else 1
            )

            # Calculate offset
            if per_page == "unlimited":
                offset = 0
                limit = total_count
            else:
                offset = (page - 1) * per_page
                limit = per_page

            # Fetch paginated entries
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Build search condition
                    search_condition = ""
                    search_params = []
                    if search:
                        search_condition = """
                            AND (content LIKE %s OR personal_thought LIKE %s OR 
                                 interaction_summary LIKE %s OR user_message LIKE %s OR
                                 JSON_EXTRACT(emotions, '$[*].type') LIKE %s)
                        """
                        search_term = f"%{search}%"
                        search_params = [
                            search_term,
                            search_term,
                            search_term,
                            search_term,
                            search_term,
                        ]

                    if include_archived:
                        # Get entries from both tables, ordered by timestamp DESC
                        # Note: try to include involved_users if column exists
                        try:
                            query = f"""
                                (SELECT id, content, personal_thought, timestamp, context_tags, involved_users, 
                                       emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                                       FALSE as archived
                                FROM ai_diary
                                WHERE 1=1 {search_condition})
                                UNION ALL
                                (SELECT id, content, personal_thought, timestamp, context_tags, involved_users, 
                                       emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                                       TRUE as archived
                                FROM ai_diary_archive
                                WHERE 1=1 {search_condition})
                                ORDER BY timestamp DESC
                                LIMIT %s OFFSET %s
                            """
                            await cur.execute(query, search_params + [limit, offset])
                        except Exception as e:
                            # If involved_users column doesn't exist, fallback to query without it
                            if "Unknown column" in str(e):
                                query = f"""
                                    (SELECT id, content, personal_thought, timestamp, context_tags, '[]' as involved_users, 
                                           emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                                           FALSE as archived
                                    FROM ai_diary
                                    WHERE 1=1 {search_condition})
                                    UNION ALL
                                    (SELECT id, content, personal_thought, timestamp, context_tags, '[]' as involved_users, 
                                           emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                                           TRUE as archived
                                    FROM ai_diary_archive
                                    WHERE 1=1 {search_condition})
                                    ORDER BY timestamp DESC
                                    LIMIT %s OFFSET %s
                                """
                                await cur.execute(
                                    query, search_params + [limit, offset]
                                )
                            else:
                                raise
                    else:
                        try:
                            query = f"""
                                SELECT id, content, personal_thought, timestamp, context_tags, involved_users, 
                                       emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                                       FALSE as archived
                                FROM ai_diary
                                WHERE 1=1 {search_condition}
                                ORDER BY timestamp DESC
                                LIMIT %s OFFSET %s
                            """
                            await cur.execute(query, search_params + [limit, offset])
                        except Exception as e:
                            # If involved_users column doesn't exist, fallback
                            if "Unknown column" in str(e):
                                query = f"""
                                    SELECT id, content, personal_thought, timestamp, context_tags, '[]' as involved_users, 
                                           emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                                           FALSE as archived
                                    FROM ai_diary
                                    WHERE 1=1 {search_condition}
                                    ORDER BY timestamp DESC
                                    LIMIT %s OFFSET %s
                                """
                                await cur.execute(
                                    query, search_params + [limit, offset]
                                )
                            else:
                                raise

                    rows = await cur.fetchall()

            # Convert rows to entries format
            entries = []
            for row in rows:
                ts = row[3]
                entry = {
                    "id": row[0],
                    "content": row[1],
                    "personal_thought": row[2],
                    "timestamp": self._dt_to_utc_iso(ts),
                    "context_tags": json.loads(row[4] or "[]"),
                    "involved_users": json.loads(row[5] or "[]"),
                    "emotions": json.loads(row[6] or "[]"),
                    "interface": row[7],
                    "chat_id": row[8],
                    "thread_id": row[9],
                    "interaction_summary": row[10],
                    "user_message": row[11],
                    "archived": row[12],
                }
                entries.append(entry)

            payload["entries"] = entries
            payload["count"] = len(entries)
            payload["available"] = plugin_enabled and bool(total_count)

            # Calculate timestamps from current page (not all entries)
            timestamps = [
                entry.get("timestamp") for entry in entries if entry.get("timestamp")
            ]
            if timestamps:
                payload["earliest_timestamp"] = min(timestamps)
                payload["latest_timestamp"] = max(timestamps)

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to fetch diary entries: {exc}")
            payload["error"] = str(exc)
            return payload

        return payload

    async def archive_diary_entries(self, request: Request):
        """Archive selected diary entries."""
        try:
            payload = await request.json()
            entry_ids = payload.get("entry_ids", [])

            if not entry_ids:
                raise HTTPException(status_code=400, detail="No entry IDs provided")

            from plugins import ai_diary

            result = ai_diary.archive_diary_entries(entry_ids)

            if result.get("success"):
                return JSONResponse(
                    {"success": True, "archived_count": result.get("archived_count", 0)}
                )
            else:
                raise HTTPException(
                    status_code=500, detail=result.get("error", "Archive failed")
                )

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to archive diary entries: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))

    async def unarchive_diary_entries(self, request: Request):
        """Unarchive selected diary entries."""
        try:
            payload = await request.json()
            entry_ids = payload.get("entry_ids", [])

            if not entry_ids:
                raise HTTPException(status_code=400, detail="No entry IDs provided")

            from plugins import ai_diary

            result = ai_diary.unarchive_diary_entries(entry_ids)

            if result.get("success"):
                return JSONResponse(
                    {
                        "success": True,
                        "unarchived_count": result.get("unarchived_count", 0),
                    }
                )
            else:
                raise HTTPException(
                    status_code=500, detail=result.get("error", "Unarchive failed")
                )

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to unarchive diary entries: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))

    async def delete_archived_entries(self, request: Request):
        """Delete archived diary entries permanently."""
        try:
            payload = await request.json()
            entry_ids = payload.get("entry_ids", [])

            if not entry_ids:
                raise HTTPException(status_code=400, detail="No entry IDs provided")

            from plugins import ai_diary

            result = ai_diary.delete_archived_entries(entry_ids)

            if result.get("success"):
                return JSONResponse(
                    {"success": True, "deleted_count": result.get("deleted_count", 0)}
                )
            else:
                raise HTTPException(
                    status_code=500, detail=result.get("error", "Delete failed")
                )

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to delete archived diary entries: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))

    async def history_diary(self, request: Request):
        """Return one entry per day for the History > Diary sub-tab (daily consolidated view)."""
        params = request.query_params

        def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            return max(minimum, min(maximum, parsed))

        page = _bounded_int(params.get("page"), default=1, minimum=1, maximum=1000)
        per_page = _bounded_int(
            params.get("per_page"), default=30, minimum=1, maximum=60
        )
        search = params.get("search", "").strip()
        sort = params.get("sort", "desc")

        try:
            from core.db import _get_db_type, get_conn_ctx

            offset = (page - 1) * per_page
            order = "DESC" if sort == "desc" else "ASC"
            is_postgres = _get_db_type() == "postgres"

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # MySQL/MariaDB truncates GROUP_CONCAT aggressively by default.
                    # Postgres uses translated string_agg and does not support this SET.
                    if not is_postgres:
                        await cur.execute("SET SESSION group_concat_max_len = 1048576")

                    if search:
                        search_term = f"%{search}%"
                        where_clause = (
                            "WHERE (content LIKE %s OR interaction_summary LIKE %s)"
                        )
                        search_params: list = [search_term, search_term]
                    else:
                        where_clause = ""
                        search_params = []

                    # Count distinct days that match the filter
                    count_query = f"""
                        SELECT COUNT(DISTINCT DATE(timestamp))
                        FROM ai_diary
                        {where_clause}
                    """
                    await cur.execute(count_query, search_params)
                    count_row = await cur.fetchone()
                    total_count = count_row[0] if count_row else 0

                    # Aggregate ALL rows for each day — GROUP_CONCAT handles both
                    # old multi-row days and new single-row upserted days correctly.
                    # The '---' separator matches what _upsert_diary_impl uses, so
                    # the frontend can split on it to render fragments.
                    query = f"""
                        SELECT
                            MAX(id)                                                           AS id,
                            GROUP_CONCAT(content ORDER BY id ASC SEPARATOR '\n\n---\n\n')    AS content,
                            MAX(personal_thought)                                             AS personal_thought,
                            MAX(timestamp)                                                    AS timestamp,
                            JSON_EXTRACT(MAX(emotions), '$[0].type')                          AS primary_emotion
                        FROM ai_diary
                        {where_clause}
                        GROUP BY DATE(timestamp)
                        ORDER BY MAX(timestamp) {order}
                        LIMIT %s OFFSET %s
                    """
                    await cur.execute(query, search_params + [per_page, offset])
                    rows = await cur.fetchall()

            entries = []
            for row in rows:
                entries.append(
                    {
                        "id": row[0],
                        "content": row[1],
                        "personal_thought": row[2],
                        "timestamp": self._dt_to_utc_iso(row[3]),
                        "primary_emotion": row[4],
                    }
                )

            total_pages = (
                (total_count + per_page - 1) // per_page if total_count > 0 else 1
            )

            return JSONResponse(
                {
                    "success": True,
                    "entries": entries,
                    "page": page,
                    "per_page": per_page,
                    "total_count": total_count,
                    "total_pages": total_pages,
                }
            )

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to fetch daily diary: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def history_grillo(self, request: Request):
        """Return grillo activity log for the History > Grillo sub-tab - optimized."""
        params = request.query_params

        def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            return max(minimum, min(maximum, parsed))

        page = _bounded_int(params.get("page"), default=1, minimum=1, maximum=1000)
        per_page = _bounded_int(
            params.get("per_page"), default=15, minimum=1, maximum=50
        )  # Ridotto
        search = params.get("search", "").strip()
        beat_type_filter = params.get("beat_type", "").strip()
        sort = params.get("sort", "desc")

        try:
            from core.db import get_conn_ctx

            offset = (page - 1) * per_page
            order = "DESC" if sort == "desc" else "ASC"

            # Build WHERE clause
            where_conditions = []
            where_params = []

            if search:
                where_conditions.append(
                    "beat_type LIKE %s"
                )  # Removed prompt_text search for speed
                where_params.append(f"%{search}%")

            if beat_type_filter:
                where_conditions.append("beat_type = %s")
                where_params.append(beat_type_filter)

            where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"

            # Fetch entries WITHOUT expensive LEFT JOIN - load diary content on-demand if needed
            entries = []
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Include a lightweight join to ai_diary so the UI can show the actual
                    # reflective text when response_text is missing/empty.
                    query = f"""
                        SELECT g.id,
                               g.beat_type,
                               g.prompt_text as prompt_text,
                               g.response_text as response_text,
                               g.diary_entry_id,
                               g.executed_at,
                               d.content as diary_content
                        FROM grillo_activity_log g
                        LEFT JOIN ai_diary d ON g.diary_entry_id = d.id
                        WHERE {where_clause}
                        ORDER BY g.executed_at {order}
                        LIMIT %s OFFSET %s
                    """

                    await cur.execute(query, where_params + [per_page + 1, offset])
                    rows = await cur.fetchall()

                    has_more = len(rows) > per_page
                    if has_more:
                        rows = rows[:per_page]

                    for row in rows:
                        # Emit explicit UTC timestamps; naive datetimes are assumed local TZ.
                        executed_at_str = self._dt_to_utc_iso(row[5])

                        entries.append(
                            {
                                "id": row[0],
                                "beat_type": row[1],
                                "prompt_text": row[2],  # Truncated for speed
                                "response_text": row[3],  # Truncated LLM response
                                "diary_entry_id": row[4],
                                "executed_at": executed_at_str,
                                "has_diary": row[4]
                                is not None,  # Flag instead of content
                                "diary_content": row[6],
                            }
                        )

            # Lazy-load list of available beat types (for populating the UI selector)
            beat_types = []
            if page == 1 and not beat_type_filter:
                async with get_conn_ctx() as conn:
                    async with conn.cursor() as cur:
                        # Fetch a large set of distinct beat types for the UI filter
                        await cur.execute(
                            "SELECT DISTINCT beat_type FROM grillo_activity_log ORDER BY beat_type LIMIT 1000"
                        )
                        rows_bt = await cur.fetchall()
                        # Normalize by trimming whitespace and ignore NULLs
                        beat_types = [
                            row[0].strip()
                            for row in rows_bt
                            if row[0] and str(row[0]).strip()
                        ]

            # Estimate total
            total_count = offset + len(rows) + (per_page if has_more else 0)
            total_pages = (total_count + per_page - 1) // per_page

            # Attach per-action execs to entries
            try:
                from core.core_initializer import PLUGIN_REGISTRY

                grillo_plugin = None
                try:
                    if isinstance(PLUGIN_REGISTRY, dict):
                        grillo_plugin = PLUGIN_REGISTRY.get(
                            "grillo_plugin"
                        ) or PLUGIN_REGISTRY.get("grillo_impl")
                except Exception:
                    grillo_plugin = None

                activity_ids = [e["id"] for e in entries]
                action_map: dict[int, list] = {}
                if grillo_plugin and hasattr(grillo_plugin, "fetch_action_execs"):
                    action_map = (
                        await grillo_plugin.fetch_action_execs(activity_ids)
                        if activity_ids
                        else {}
                    )
                else:
                    try:
                        from plugins.grillo.grillo_impl import GrilloPlugin

                        action_map = (
                            await GrilloPlugin.fetch_action_execs(activity_ids)
                            if activity_ids
                            else {}
                        )
                    except Exception as exc:
                        log_debug(f"[webui] fetch_action_execs failed: {exc}")
                for e in entries:
                    e["actions"] = action_map.get(e["id"], [])
            except Exception as e:
                log_debug(f"[webui] fetch_action_execs failed: {e}")

            return JSONResponse(
                {
                    "success": True,
                    "entries": entries,
                    "beat_types": beat_types,
                    "page": page,
                    "per_page": per_page,
                    "total_count": total_count,
                    "total_pages": total_pages,
                }
            )

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to fetch grillo history: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def _fetch_calendar_event_rows(self) -> list[dict[str, Any]]:
        """Fetch all ``scheduled_events`` rows as plain dicts for calendar use."""
        from core.db import get_conn_ctx

        rows: list[dict[str, Any]] = []
        columns = [
            "id",
            "date",
            "time",
            "recurrence_type",
            "next_run",
            "description",
            "created_at",
            "created_by",
            "uid",
            "rrule",
            "tzid",
            "source",
            "delivered",
        ]
        col_list = ", ".join(columns)
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {col_list} FROM scheduled_events ORDER BY next_run ASC"
                )
                fetched = await cur.fetchall()
                for row in fetched:
                    rows.append(dict(zip(columns, row)))
        return rows

    async def history_calendar(self, request: Request):
        """Return event occurrences within a requested month for the calendar view.

        Expands recurring events (via their RRULE) into per-occurrence entries
        that fall inside the requested month window. Each entry carries a local
        ``date`` (YYYY-MM-DD) and ``time`` (HH:MM) so the frontend grid can place
        it without any further timezone maths.
        """
        params = request.query_params

        def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            return max(minimum, min(maximum, parsed))

        from datetime import datetime as _dt

        now_local = _dt.now()
        year = _bounded_int(
            params.get("year"), default=now_local.year, minimum=1970, maximum=3000
        )
        month = _bounded_int(
            params.get("month"), default=now_local.month, minimum=1, maximum=12
        )

        try:
            from datetime import timedelta
            import recurring_ical_events

            from core.calendar_utils import build_calendar
            from core.time_zone_utils import get_local_timezone

            system_tz = get_local_timezone()

            # Month window in the local timezone (inclusive start, exclusive end).
            window_start = _dt(year, month, 1, tzinfo=system_tz)
            if month == 12:
                window_end = _dt(year + 1, 1, 1, tzinfo=system_tz)
            else:
                window_end = _dt(year, month + 1, 1, tzinfo=system_tz)

            from core.calendar_utils import build_event_uid

            rows = await self._fetch_calendar_event_rows()
            # Map effective UID -> row so we can annotate occurrences with
            # source/id. build_calendar synthesises ``synth-<id>@host`` when a
            # row has no stored ``uid``, so the map must key on the same value.
            row_by_uid: dict[str, dict[str, Any]] = {}
            for r in rows:
                uid = r.get("uid")
                if not uid and r.get("id") is not None:
                    uid = build_event_uid(r["id"])
                if uid:
                    row_by_uid[str(uid)] = r

            calendar = build_calendar(rows, system_tz=system_tz)

            events: list[dict[str, Any]] = []
            try:
                occurrences = recurring_ical_events.of(calendar).between(
                    window_start, window_end - timedelta(seconds=1)
                )
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} calendar expansion failed: {exc}")
                occurrences = []

            for occ in occurrences:
                try:
                    dtstart = occ.get("dtstart")
                    if dtstart is None:
                        continue
                    start_dt = dtstart.dt
                    if isinstance(start_dt, datetime):
                        if start_dt.tzinfo is None:
                            start_dt = start_dt.replace(tzinfo=system_tz)
                        local_dt = start_dt.astimezone(system_tz)
                        date_str = local_dt.strftime("%Y-%m-%d")
                        time_str = local_dt.strftime("%H:%M")
                    else:
                        # All-day (date only)
                        date_str = start_dt.strftime("%Y-%m-%d")
                        time_str = ""

                    uid = str(occ.get("uid", "")) if occ.get("uid") else ""
                    src_row = row_by_uid.get(uid, {})
                    rrule = src_row.get("rrule") or (occ.get("rrule") is not None)
                    events.append(
                        {
                            "id": src_row.get("id"),
                            "uid": uid,
                            "date": date_str,
                            "time": time_str,
                            "description": str(occ.get("summary", "")),
                            "recurring": bool(rrule),
                            "recurrence_type": src_row.get("recurrence_type") or "none",
                            "source": src_row.get("source") or "synth",
                            "delivered": bool(src_row.get("delivered")),
                        }
                    )
                except Exception as exc:
                    log_debug(f"{LOG_PREFIX} skipping calendar occurrence: {exc}")
                    continue

            # Merge in occurrences from subscribed external calendars (ICS/CalDAV)
            # so they show up in the grid alongside internal events. External
            # occurrences are read-only: they carry an ``external:<id>`` source
            # and no internal ``id``, so the frontend hides edit/delete controls.
            try:
                from core.external_calendars import gather_all_external_occurrences

                external = await gather_all_external_occurrences(
                    window_start=window_start, window_end=window_end
                )
                for occ in external:
                    try:
                        start_dt = occ.get("start")
                        if start_dt is None:
                            continue
                        if occ.get("all_day"):
                            date_str = start_dt.strftime("%Y-%m-%d")
                            time_str = ""
                        else:
                            if start_dt.tzinfo is None:
                                start_dt = start_dt.replace(tzinfo=system_tz)
                            local_dt = start_dt.astimezone(system_tz)
                            date_str = local_dt.strftime("%Y-%m-%d")
                            time_str = local_dt.strftime("%H:%M")
                        events.append(
                            {
                                "id": None,
                                "uid": str(occ.get("uid") or ""),
                                "date": date_str,
                                "time": time_str,
                                "description": str(occ.get("summary") or ""),
                                "recurring": False,
                                "recurrence_type": "none",
                                "source": str(occ.get("source") or "external"),
                                "calendar_name": str(occ.get("calendar_name") or ""),
                                "delivered": False,
                            }
                        )
                    except Exception as exc:
                        log_debug(
                            f"{LOG_PREFIX} skipping external calendar occurrence: {exc}"
                        )
                        continue
            except Exception as exc:
                log_warning(
                    f"{LOG_PREFIX} failed to gather external calendar occurrences: {exc}"
                )

            return JSONResponse({"success": True, "events": events})

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to fetch calendar: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def history_calendar_upcoming(self, request: Request):
        """Return the next few upcoming events over a short look-ahead window.

        Purely informational: it expands recurring events (via RRULE) starting
        from *now* over the next ``days`` days and returns at most ``limit``
        occurrences, each with a preformatted ``label`` such as
        ``Jul 9, 9:00 (JST)`` for direct display.
        """
        params = request.query_params

        def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            return max(minimum, min(maximum, parsed))

        days = _bounded_int(params.get("days"), default=3, minimum=1, maximum=31)
        limit = _bounded_int(params.get("limit"), default=5, minimum=1, maximum=20)

        try:
            from datetime import datetime as _dt
            from datetime import timedelta
            import recurring_ical_events

            from core.calendar_utils import build_calendar
            from core.time_zone_utils import get_local_timezone

            system_tz = get_local_timezone()
            window_start = _dt.now(tz=system_tz)
            window_end = window_start + timedelta(days=days)

            from core.calendar_utils import build_event_uid

            rows = await self._fetch_calendar_event_rows()
            row_by_uid: dict[str, dict[str, Any]] = {}
            for r in rows:
                uid = r.get("uid")
                if not uid and r.get("id") is not None:
                    uid = build_event_uid(r["id"])
                if uid:
                    row_by_uid[str(uid)] = r

            calendar = build_calendar(rows, system_tz=system_tz)

            try:
                occurrences = recurring_ical_events.of(calendar).between(
                    window_start, window_end
                )
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} upcoming expansion failed: {exc}")
                occurrences = []

            collected: list[dict[str, Any]] = []
            for occ in occurrences:
                try:
                    dtstart = occ.get("dtstart")
                    if dtstart is None:
                        continue
                    start_dt = dtstart.dt
                    if isinstance(start_dt, datetime):
                        if start_dt.tzinfo is None:
                            start_dt = start_dt.replace(tzinfo=system_tz)
                        local_dt = start_dt.astimezone(system_tz)
                        all_day = False
                    else:
                        # All-day (date only) -> anchor at local midnight.
                        local_dt = _dt(
                            start_dt.year,
                            start_dt.month,
                            start_dt.day,
                            tzinfo=system_tz,
                        )
                        all_day = True

                    if local_dt < window_start:
                        continue

                    if all_day:
                        label = f"{local_dt.strftime('%b %-d')} (all day)"
                    else:
                        tz_abbr = local_dt.strftime("%Z") or "local"
                        label = (
                            f"{local_dt.strftime('%b %-d')}, "
                            f"{local_dt.hour}:{local_dt.strftime('%M')} ({tz_abbr})"
                        )

                    uid = str(occ.get("uid", "")) if occ.get("uid") else ""
                    src_row = row_by_uid.get(uid, {})
                    collected.append(
                        {
                            "sort_key": local_dt.timestamp(),
                            "label": label,
                            "description": str(occ.get("summary", "")),
                            "source": src_row.get("source") or "synth",
                            "all_day": all_day,
                        }
                    )
                except Exception as exc:
                    log_debug(f"{LOG_PREFIX} skipping upcoming occurrence: {exc}")
                    continue

            collected.sort(key=lambda e: e["sort_key"])
            trimmed = [
                {k: v for k, v in e.items() if k != "sort_key"}
                for e in collected[:limit]
            ]
            return JSONResponse({"success": True, "events": trimmed, "days": days})

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to fetch upcoming events: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def create_calendar_event(self, request: Request):
        """Create a new internal scheduled event from the WebUI calendar.

        The event is stored exactly like a Synth-created reminder. When it fires
        it is delivered to the Synth as an internal ``scheduled_reminder`` beat,
        which then decides whether/how/who to contact.
        """
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"success": False, "error": "Invalid JSON body"}, status_code=400
            )

        if not isinstance(payload, dict):
            return JSONResponse(
                {"success": False, "error": "Invalid payload"}, status_code=400
            )

        date = str(payload.get("date", "")).strip()
        time_val = str(payload.get("time", "")).strip() or "00:00"
        recurrence = str(payload.get("recurrence", "none")).strip() or "none"
        description = str(payload.get("description", "")).strip()

        if not date or not description:
            return JSONResponse(
                {"success": False, "error": "date and description are required"},
                status_code=400,
            )
        if recurrence not in ("none", "daily", "weekly", "monthly", "always"):
            recurrence = "none"

        try:
            from core.db import insert_scheduled_event

            await insert_scheduled_event(
                date=date,
                time=time_val,
                recurrence_type=recurrence,
                description=description,
                created_by="user",
                source="user",
            )
            return JSONResponse({"success": True})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to create calendar event: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def update_calendar_event(self, request: Request):
        """Update an existing internal scheduled event from the WebUI calendar.

        Only internal events (``source`` not starting with ``external``) can be
        edited; external calendar occurrences are read-only mirrors.
        """
        event_id_raw = request.path_params.get("event_id")
        try:
            event_id = int(event_id_raw)
        except (TypeError, ValueError):
            return JSONResponse(
                {"success": False, "error": "Invalid event id"}, status_code=400
            )

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"success": False, "error": "Invalid JSON body"}, status_code=400
            )

        if not isinstance(payload, dict):
            return JSONResponse(
                {"success": False, "error": "Invalid payload"}, status_code=400
            )

        date = str(payload.get("date", "")).strip()
        time_val = str(payload.get("time", "")).strip() or "00:00"
        recurrence = str(payload.get("recurrence", "none")).strip() or "none"
        description = str(payload.get("description", "")).strip()

        if not date or not description:
            return JSONResponse(
                {"success": False, "error": "date and description are required"},
                status_code=400,
            )
        if recurrence not in ("none", "daily", "weekly", "monthly", "always"):
            recurrence = "none"

        # Optional processed/delivered flag from the editor. When the key is
        # absent, keep the legacy reset-to-not-delivered behaviour (None).
        delivered_raw = payload.get("delivered", None)
        delivered: bool | None = None if delivered_raw is None else bool(delivered_raw)

        try:
            from core.db import get_conn_ctx, update_scheduled_event

            # Guard: refuse to edit external (read-only) occurrences.
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT source FROM scheduled_events WHERE id = %s",
                        (event_id,),
                    )
                    row = await cur.fetchone()
            if row is None:
                return JSONResponse(
                    {"success": False, "error": "Event not found"}, status_code=404
                )
            source = str(row[0] or "")
            if source.startswith("external"):
                return JSONResponse(
                    {
                        "success": False,
                        "error": "External calendar events are read-only",
                    },
                    status_code=403,
                )

            updated = await update_scheduled_event(
                event_id=event_id,
                date=date,
                time=time_val,
                recurrence_type=recurrence,
                description=description,
                delivered=delivered,
            )
            if not updated:
                return JSONResponse(
                    {"success": False, "error": "Event not found or unchanged"},
                    status_code=404,
                )
            return JSONResponse({"success": True})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to update calendar event: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def delete_calendar_event(self, request: Request):
        """Delete a scheduled event by id (WebUI calendar)."""
        event_id_raw = request.path_params.get("event_id")
        try:
            event_id = int(event_id_raw)
        except (TypeError, ValueError):
            return JSONResponse(
                {"success": False, "error": "Invalid event id"}, status_code=400
            )

        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM scheduled_events WHERE id = %s", (event_id,)
                    )
            return JSONResponse({"success": True})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to delete calendar event: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def export_calendar_ics(self, request: Request):
        """Serve all scheduled events as a single iCalendar (.ics) file.

        This endpoint powers both the "Download .ics" button and the
        ``webcal://`` subscription (Google Calendar, Apple Calendar, ...).
        """
        try:
            from core.calendar_utils import build_calendar
            from core.time_zone_utils import get_local_timezone

            system_tz = get_local_timezone()
            rows = await self._fetch_calendar_event_rows()
            calendar = build_calendar(rows, system_tz=system_tz)
            ics_bytes = calendar.to_ical()

            return Response(
                content=ics_bytes,
                media_type="text/calendar; charset=utf-8",
                headers={
                    "Content-Disposition": 'attachment; filename="synth-calendar.ics"'
                },
            )
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to export calendar ICS: {exc}")
            return Response(
                content=f"Calendar export failed: {exc}",
                media_type="text/plain",
                status_code=500,
            )

    async def list_external_calendars(self, request: Request):
        """Return the configured external calendar subscriptions.

        Credentials are never exposed: the encrypted password column is
        stripped and replaced with a boolean ``has_password`` flag.
        """
        try:
            from core.external_calendars import (
                ensure_external_calendars_table,
                list_external_calendars,
            )

            await ensure_external_calendars_table()
            rows = await list_external_calendars()
            safe_rows: list[dict[str, Any]] = []
            for row in rows:
                safe: dict[str, Any] = {}
                for k, v in row.items():
                    if k == "password_enc":
                        continue
                    safe[k] = v.isoformat() if isinstance(v, datetime) else v
                safe["has_password"] = bool(row.get("password_enc"))
                safe_rows.append(safe)
            return JSONResponse({"calendars": safe_rows})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to list external calendars: {exc}")
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def list_known_interface_paths(self, request: Request) -> JSONResponse:
        """Return known interface paths with a human-friendly label.

        Used by the calendar event editor to let the user attach a delivery
        target to a scheduled reminder. Each entry is derived from the most
        recent chat message seen for that ``interface_path`` so the label
        reflects the last known sender name (pretty name).
        """
        try:
            from core.db import get_conn_ctx

            entries: list[dict[str, str]] = []
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT DISTINCT ON (interface_path)
                            interface_path, sender_name
                        FROM chat_history_cache
                        ORDER BY interface_path, timestamp DESC
                        """
                    )
                    rows = await cur.fetchall()
                    for row in rows:
                        if isinstance(row, dict):
                            path = row.get("interface_path")
                            sender = row.get("sender_name")
                        else:
                            path = row[0]
                            sender = row[1] if len(row) > 1 else None
                        if not path:
                            continue
                        label = f"{sender} ({path})" if sender else str(path)
                        entries.append({"interface_path": str(path), "label": label})

            entries.sort(key=lambda item: item["label"].lower())
            return JSONResponse({"success": True, "interface_paths": entries})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to list interface paths: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def add_external_calendar(self, request: Request):
        """Add a new external calendar subscription (CalDAV or ICS)."""
        try:
            from core.external_calendars import (
                CALENDAR_TYPES,
                add_external_calendar,
                ensure_external_calendars_table,
            )

            body = await request.json()
            name = (body.get("name") or "").strip()
            url = (body.get("url") or "").strip()
            cal_type = (body.get("cal_type") or "ics").strip().lower()
            username = body.get("username") or None
            password = body.get("password") or None
            enabled = bool(body.get("enabled", True))

            if not name or not url:
                return JSONResponse(
                    {"error": "name and url are required"}, status_code=400
                )
            if cal_type not in CALENDAR_TYPES:
                return JSONResponse(
                    {"error": f"cal_type must be one of {sorted(CALENDAR_TYPES)}"},
                    status_code=400,
                )

            await ensure_external_calendars_table()
            new_id = await add_external_calendar(
                name=name,
                url=url,
                cal_type=cal_type,
                username=username,
                password=password,
                enabled=enabled,
            )
            if new_id is None:
                return JSONResponse(
                    {"error": "Failed to add calendar"}, status_code=500
                )
            return JSONResponse({"id": new_id})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to add external calendar: {exc}")
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def delete_external_calendar(self, request: Request):
        """Delete an external calendar and its materialised events."""
        try:
            from core.external_calendars import (
                delete_external_calendar,
                ensure_external_calendars_table,
            )

            calendar_id = int(request.path_params["calendar_id"])
            await ensure_external_calendars_table()
            ok = await delete_external_calendar(calendar_id)
            if not ok:
                return JSONResponse({"error": "Calendar not found"}, status_code=404)
            return JSONResponse({"deleted": calendar_id})
        except (ValueError, KeyError):
            return JSONResponse({"error": "Invalid calendar id"}, status_code=400)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to delete external calendar: {exc}")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @staticmethod
    def _extract_dream_text(response_text: Any) -> str | None:
        """Extract the readable dream text from a dream beat's response_text.

        Dream beats store the LLM output as a JSON action envelope in
        grillo_activity_log.response_text, where the actual dream lives in the
        create_personal_diary_entry action's payload.content. The linked diary
        entry (diary_entry_id) is a separate interaction diary, NOT the dream,
        so we must read the dream text from here.
        """
        if not response_text or not isinstance(response_text, str):
            return None
        import json as _json

        try:
            data = _json.loads(response_text)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        actions = data.get("actions")
        if not isinstance(actions, list):
            return None
        for action in actions:
            if not isinstance(action, dict):
                continue
            payload = action.get("payload")
            if isinstance(payload, dict):
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
        return None

    async def history_dreams(self, request: Request):
        """Return dream beats for the History > Dreams sub-tab."""
        params = request.query_params

        def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            return max(minimum, min(maximum, parsed))

        page = _bounded_int(params.get("page"), default=1, minimum=1, maximum=1000)
        per_page = _bounded_int(
            params.get("per_page"), default=15, minimum=1, maximum=50
        )
        search = params.get("search", "").strip()
        sort = params.get("sort", "desc")

        try:
            from core.db import get_conn_ctx

            offset = (page - 1) * per_page
            order = "DESC" if sort == "desc" else "ASC"

            where_conditions = ["g.beat_type = %s"]
            where_params: list[Any] = ["dream"]

            if search:
                # The dream text lives inside g.response_text (JSON action payload),
                # not in the linked diary entry, so search there.
                where_conditions.append("g.response_text LIKE %s")
                where_params.append(f"%{search}%")

            where_clause = " AND ".join(where_conditions)

            entries = []
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    query = f"""
                        SELECT g.id,
                               g.response_text as response_text,
                               g.diary_entry_id,
                               g.executed_at,
                               d.content as diary_content
                        FROM grillo_activity_log g
                        LEFT JOIN ai_diary d ON g.diary_entry_id = d.id
                        WHERE {where_clause}
                        ORDER BY g.executed_at {order}
                        LIMIT %s OFFSET %s
                    """

                    await cur.execute(query, where_params + [per_page + 1, offset])
                    rows = await cur.fetchall()

                    has_more = len(rows) > per_page
                    if has_more:
                        rows = rows[:per_page]

                    for row in rows:
                        executed_at_str = self._dt_to_utc_iso(row[3])
                        dream_text = self._extract_dream_text(row[1]) or row[4] or ""
                        entries.append(
                            {
                                "id": row[0],
                                "content": dream_text,
                                "diary_entry_id": row[2],
                                "executed_at": executed_at_str,
                                "has_diary": row[2] is not None,
                            }
                        )

            total_count = offset + len(rows) + (per_page if has_more else 0)
            total_pages = (total_count + per_page - 1) // per_page

            return JSONResponse(
                {
                    "success": True,
                    "entries": entries,
                    "page": page,
                    "per_page": per_page,
                    "total_count": total_count,
                    "total_pages": total_pages,
                }
            )

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to fetch dreams history: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def history_chat(self, request: Request):
        """Return chat history for the History > Chat sub-tab - optimized."""
        params = request.query_params

        def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            return max(minimum, min(maximum, parsed))

        page = _bounded_int(params.get("page"), default=1, minimum=1, maximum=1000)
        per_page = _bounded_int(
            params.get("per_page"), default=30, minimum=1, maximum=100
        )  # Ridotto da 50->30, max da 200->100
        interface_path = params.get("interface_path", "").strip()
        search = params.get("search", "").strip()
        sort = params.get("sort", "desc")

        try:
            from core.db import get_conn_ctx

            offset = (page - 1) * per_page
            order = "DESC" if sort == "desc" else "ASC"

            # Build WHERE clause
            where_conditions = []
            where_params = []

            if interface_path:
                where_conditions.append("interface_path = %s")
                where_params.append(interface_path)

            if search:
                where_conditions.append(
                    "message_text LIKE %s"
                )  # Removed sender_name for speed
                where_params.append(f"%{search}%")

            where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"

            # Fetch messages with LIMIT + 1 trick
            messages = []
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    query = f"""
                        SELECT interface_path, sender_name, message_text, timestamp, metadata
                        FROM chat_history_cache
                        WHERE {where_clause}
                        ORDER BY timestamp {order}
                        LIMIT %s OFFSET %s
                    """

                    await cur.execute(query, where_params + [per_page + 1, offset])
                    rows = await cur.fetchall()

                    has_more = len(rows) > per_page
                    if has_more:
                        rows = rows[:per_page]

                    for row in rows:
                        import json as _json

                        if isinstance(row, dict):
                            raw_interface_path = row.get("interface_path")
                            raw_sender_name = row.get("sender_name")
                            raw_message_text = row.get("message_text")
                            raw_timestamp = row.get("timestamp")
                            raw_meta = row.get("metadata")
                        else:
                            raw_interface_path = row[0]
                            raw_sender_name = row[1]
                            raw_message_text = row[2]
                            raw_timestamp = row[3]
                            raw_meta = row[4] if len(row) > 4 else None

                        timestamp_str = self._dt_to_utc_iso(raw_timestamp)
                        parsed_meta = _json.loads(raw_meta) if raw_meta else None
                        messages.append(
                            {
                                "interface_path": raw_interface_path,
                                "sender_name": raw_sender_name,
                                "message_text": raw_message_text,
                                "timestamp": timestamp_str,
                                "metadata": parsed_meta,
                            }
                        )

            # Lazy load interface_paths only when needed (not on every request)
            interface_paths = []
            if page == 1 and not interface_path:  # Only on first load without filter
                async with get_conn_ctx() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT DISTINCT interface_path FROM chat_history_cache ORDER BY interface_path LIMIT 50"
                        )
                        rows = await cur.fetchall()
                        interface_paths = [row[0] for row in rows]

            # Estimate total (same approach as grillo)
            total_count = offset + len(rows) + (per_page if has_more else 0)
            total_pages = (total_count + per_page - 1) // per_page

            return JSONResponse(
                {
                    "success": True,
                    "messages": messages,
                    "interface_paths": interface_paths,
                    "page": page,
                    "per_page": per_page,
                    "total_count": total_count,
                    "total_pages": total_pages,
                }
            )

        except Exception as exc:
            error_text = str(exc)
            lowered_error = error_text.lower()
            if (
                "asyncpg is not installed" in lowered_error
                or "aiomysql is not installed" in lowered_error
                or "db unavailable" in lowered_error
            ):
                log_warning(
                    f"{LOG_PREFIX} Chat history DB unavailable; returning empty history payload: {exc}"
                )
                return JSONResponse(
                    {
                        "success": True,
                        "messages": [],
                        "interface_paths": [],
                        "page": page,
                        "per_page": per_page,
                        "total_count": 0,
                        "total_pages": 0,
                    }
                )

            log_error(f"{LOG_PREFIX} Failed to fetch chat history: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def list_log_failures(self, request: Request):
        """Return paginated failure events for the Logs > Failures sub-tab."""
        params = request.query_params

        def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            return max(minimum, min(maximum, parsed))

        page = _bounded_int(params.get("page"), default=1, minimum=1, maximum=1000)
        per_page = _bounded_int(
            params.get("per_page"), default=20, minimum=1, maximum=100
        )
        search = params.get("search", "").strip()
        failure_code = params.get("failure_code", "").strip()
        stage = params.get("stage", "").strip()
        sort = params.get("sort", "desc")

        try:
            from core.llm_failure_log import list_failure_entries

            payload = await list_failure_entries(
                page=page,
                per_page=per_page,
                search=search,
                failure_code=failure_code,
                stage=stage,
                sort=sort,
            )

            entries = []
            for entry in payload.get("entries", []):
                normalized = dict(entry)
                normalized["created_at"] = self._dt_to_utc_iso(entry.get("created_at"))
                entries.append(normalized)

            return JSONResponse(
                {
                    "success": True,
                    "entries": entries,
                    "page": payload.get("page", page),
                    "per_page": payload.get("per_page", per_page),
                    "total_count": payload.get("total_count", 0),
                    "total_pages": payload.get("total_pages", 1),
                }
            )
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to fetch log failures: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def delete_log_failure(self, failure_id: int):
        """Delete a persistent failure-log entry."""
        try:
            from core.llm_failure_log import delete_failure_entry

            deleted = await delete_failure_entry(failure_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="Failure entry not found")
            return JSONResponse({"success": True, "deleted": True, "id": failure_id})
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to delete log failure {failure_id}: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Chat archive endpoints (filesystem-backed)
    # ------------------------------------------------------------------
    async def archive_chat(self, request: Request):
        """Archive current chat messages for the persistent session and clear them.

        Returns: { success, archive_id }
        """
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        session_id = payload.get("session_id") or self.session_id
        if not session_id:
            raise HTTPException(status_code=400, detail="No session_id available")
        try:
            from core.chat_history_cache import load_chat_history, clear_chat_history
            from core.chat_archives_db import create_archive, init_chat_archives_table
            from core.session_meta import get_session_meta
            from core.chat_context_manager import clear_chat_context

            interface_path = f"{INTERFACE_NAME}/{session_id}"
            messages_deque = await load_chat_history(interface_path)
            # Convert deque to list
            messages = list(messages_deque) if messages_deque else []
            try:
                # Ensure DB table exists
                try:
                    await init_chat_archives_table()
                except Exception:
                    pass
            except Exception:
                pass
            # Fetch current session metadata (camera state, rect) to include with archive
            try:
                metadata = await get_session_meta(interface_path)
            except Exception:
                metadata = None
            if not messages:
                log_info(
                    f"{LOG_PREFIX} Skipping archive for session {session_id}: no messages to archive"
                )
                # Clear session processing flag so clients don't keep typing indicator
                try:
                    from core.session_meta import set_session_meta as set_meta_fn

                    await set_meta_fn(interface_path, {"processing": False})
                except Exception as e:
                    log_debug(
                        f"{LOG_PREFIX} Failed to clear session processing meta after archive: {e}"
                    )
                return JSONResponse({"success": True, "saved_count": 0})

            log_info(
                f"{LOG_PREFIX} Creating archive from {len(messages)} current messages for session {session_id}"
            )
            archive = await create_archive(
                session_id, messages, name=payload.get("name"), metadata=metadata
            )
            # Clear DB cache and context
            await clear_chat_history(interface_path)
            clear_chat_context(interface_path)
            # Clear in-memory message history
            self.message_history.pop(session_id, None)
            # Also clear session processing flag so clients don't keep typing indicator
            try:
                from core.session_meta import set_session_meta as set_meta_fn

                await set_meta_fn(interface_path, {"processing": False})
            except Exception as e:
                log_debug(
                    f"{LOG_PREFIX} Failed to clear session processing meta after archive: {e}"
                )
            response = {
                "success": True,
                "archive_id": archive.get("id"),
                "saved_count": len(messages),
            }
            if archive.get("path"):
                response["path"] = archive.get("path")
            # Notify connected WebUI clients that archives changed so they can refresh
            try:
                try:
                    await self.broadcast_event(
                        "archive:changed",
                        {
                            "archive_id": archive.get("id"),
                            "session_id": session_id,
                            "saved_count": len(messages),
                        },
                    )
                except Exception as be:
                    log_debug(f"{LOG_PREFIX} Failed to broadcast archive change: {be}")
            except Exception:
                pass
            return JSONResponse(response)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to archive chat: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def list_chat_archives(self, request: Request):
        try:
            from core.chat_archives_db import list_archives

            archives = await list_archives()
            return JSONResponse({"success": True, "archives": archives})
        except Exception as exc:
            import traceback

            tb = traceback.format_exc()
            log_error(f"{LOG_PREFIX} Failed to list chat archives: {exc}\n{tb}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def get_chat_archive(self, archive_id: str):
        try:
            from core.chat_archives_db import load_archive

            arch = await load_archive(archive_id)
            return JSONResponse({"success": True, "archive": arch})
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Archive not found")
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to get chat archive {archive_id}: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def restore_chat_archive(self, request: Request):
        """Restore a previously-created chat archive to the current session.

        This will first archive the current conversation, then restore the requested archive
        by pushing messages into the chat_history_cache and memory, and replay them to client.
        """
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        archive_id = payload.get("archive_id")
        session_id = payload.get("session_id") or self.session_id
        if not archive_id:
            raise HTTPException(status_code=400, detail="Missing archive_id")
        if not session_id:
            raise HTTPException(
                status_code=400, detail="No session available to restore into"
            )
        try:
            from core.chat_archives_db import load_archive, create_archive
            from core.session_meta import set_session_meta
            from core.chat_history_cache import (
                save_chat_message,
                clear_chat_history,
                load_chat_history,
            )
            from core.chat_context_manager import (
                clear_chat_context,
                get_or_create_chat_context,
            )

            # 1) Archive current chat
            interface_path = f"{INTERFACE_NAME}/{session_id}"
            current_msgs = await load_chat_history(interface_path)
            if current_msgs:
                _ = await create_archive(session_id, list(current_msgs))
            await clear_chat_history(interface_path)
            clear_chat_context(interface_path)
            self.message_history.pop(session_id, None)
            # Ensure we clear the session processing flag when restoring to avoid stale typing indicators
            try:
                await set_session_meta(interface_path, {"processing": False})
            except Exception as e:
                log_debug(
                    f"{LOG_PREFIX} Failed to clear session processing meta after restore: {e}"
                )
            # 2) Load archive file
            log_debug(
                f"{LOG_PREFIX} restore_chat_archive called with archive_id={archive_id} session_id={session_id}"
            )
            meta = await load_archive(archive_id)
            log_info(f"{LOG_PREFIX} Loaded archive {archive_id}")
            messages = meta.get("messages", [])
            # Also restore session metadata if present
            metadata = meta.get("metadata")
            if metadata:
                try:
                    await set_session_meta(interface_path, metadata)
                except Exception as e:
                    log_debug(f"{LOG_PREFIX} Failed to restore session metadata: {e}")
            else:
                log_debug(f"{LOG_PREFIX} No session metadata in archive {archive_id}")
            # 3) Save back to DB and memory
            saved_count = 0
            for i, msg in enumerate(messages):
                text = msg.get("text") or msg.get("message_text") or ""
                sender_name = msg.get("sender_name") or msg.get("username") or "unknown"
                # Normalize aliases used for the SyntH agent to the canonical 'self'
                try:
                    if isinstance(sender_name, str) and sender_name.lower() in (
                        "synth",
                        "bot",
                        "system",
                        "synth_webui",
                    ):
                        sender_name = "self"
                except Exception:
                    pass
                sender_id = msg.get("sender_id") or msg.get("user_id") or "unknown"
                ts = msg.get("timestamp")
                try:
                    saved = await save_chat_message(
                        interface_path,
                        text,
                        sender_name=sender_name,
                        sender_id=sender_id,
                        timestamp=ts,
                    )
                    if saved:
                        saved_count += 1
                        log_debug(
                            f"{LOG_PREFIX} Saved restored message {i + 1}/{len(messages)} for session {session_id}"
                        )
                    else:
                        log_debug(
                            f"{LOG_PREFIX} Skipped saving restored message {i + 1}/{len(messages)} for session {session_id} (empty or invalid message)"
                        )
                except Exception as e:
                    log_warning(
                        f"{LOG_PREFIX} Failed to save restored message {i + 1}/{len(messages)} to cache: {e}"
                    )
            # ensure the in-memory context is repopulated
            from core.chat_context_manager import load_chat_history as ctx_load

            await ctx_load(interface_path)
            ctx = get_or_create_chat_context(interface_path)
            self.message_history[session_id] = ctx
            # 4) Replay to connected websocket if present
            try:
                await self._replay_history(session_id)
                log_info(
                    f"{LOG_PREFIX} Replayed {len(messages)} messages for session {session_id}"
                )
            except Exception as e:
                log_debug(f"{LOG_PREFIX} Failed to replay history after restore: {e}")

            # Remove the archive after successful restore so it won't be re-archived as duplicate
            deleted_archive_id = None
            # Delete only if we successfully saved at least one message
            if saved_count > 0:
                try:
                    from core.chat_archives_db import (
                        delete_archive as db_delete_archive,
                    )

                    await db_delete_archive(archive_id)
                    log_info(
                        f"{LOG_PREFIX} Deleted archive {archive_id} after successful restore"
                    )
                    deleted_archive_id = archive_id
                except Exception as e:
                    log_debug(
                        f"{LOG_PREFIX} Failed to delete archive {archive_id} after restore: {e}"
                    )
            else:
                log_warning(
                    f"{LOG_PREFIX} Restore completed but no messages were saved for archive {archive_id} (saved_count=0). Archive kept for inspection."
                )
                # Log message keys for debug purposes
                try:
                    for i, msg in enumerate(messages):
                        if isinstance(msg, dict):
                            keys = list(msg.keys())
                        else:
                            keys = [type(msg).__name__]
                        log_debug(
                            f"{LOG_PREFIX} Archive {archive_id} message {i + 1} keys: {keys}"
                        )
                except Exception as e:
                    log_debug(f"{LOG_PREFIX} Failed to log archive message keys: {e}")

            # Broadcast archive change to connected clients to prompt refresh
            try:
                try:
                    await self.broadcast_event(
                        "archive:changed",
                        {
                            "deleted_archive_id": deleted_archive_id or None,
                            "session_id": session_id,
                            "restored": len(messages),
                            "saved_count": saved_count,
                        },
                    )
                except Exception as e:
                    log_debug(
                        f"{LOG_PREFIX} Failed to broadcast archive change after restore: {e}"
                    )
            except Exception:
                pass

            # Note: do not return raw messages here to avoid double-rendering on client
            # We always replay the restored messages via WebSocket (_replay_history), so
            # the client should rely on the WebSocket replay instead of rendering
            # the API response to avoid duplicates.
            return JSONResponse(
                {
                    "success": True,
                    "restored": len(messages),
                    "saved_count": saved_count,
                    "deleted_archive_id": deleted_archive_id,
                }
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Archive not found")
        except Exception as exc:
            import traceback

            tb = traceback.format_exc()
            log_error(f"{LOG_PREFIX} Failed to restore chat archive: {exc}\n{tb}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def delete_chat_archive(self, archive_id: str):
        try:
            from core.chat_archives_db import delete_archive

            await delete_archive(archive_id)
            # Notify clients so UI can refresh
            try:
                try:
                    await self.broadcast_event(
                        "archive:changed", {"deleted_archive_id": archive_id}
                    )
                except Exception as be:
                    log_debug(f"{LOG_PREFIX} Failed to broadcast archive delete: {be}")
            except Exception:
                pass
            return JSONResponse({"success": True})
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Archive not found")
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to delete chat archive {archive_id}: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def set_session_meta(self, request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        session_id = payload.get("session_id") or self.session_id
        meta = payload.get("meta")
        if not session_id or not isinstance(meta, dict):
            raise HTTPException(status_code=400, detail="Missing session_id or meta")
        try:
            from core.session_meta import set_session_meta as set_meta_fn

            interface_path = f"{INTERFACE_NAME}/{session_id}"
            await set_meta_fn(interface_path, meta)
            return JSONResponse({"success": True})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to set session meta: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def get_session_meta(self, request: Request):
        session_id = request.query_params.get("session_id") or self.session_id
        if not session_id:
            raise HTTPException(status_code=400, detail="Missing session_id")
        try:
            from core.session_meta import get_session_meta as get_meta_fn

            interface_path = f"{INTERFACE_NAME}/{session_id}"
            meta = await get_meta_fn(interface_path)
            return JSONResponse({"success": True, "meta": meta or {}})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to get session meta: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def rename_chat_archive(self, archive_id: str, request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        new_name = payload.get("name")
        if not new_name:
            raise HTTPException(status_code=400, detail="Missing name")
        try:
            from core.chat_archives_db import rename_archive

            meta = await rename_archive(archive_id, new_name)
            return JSONResponse(
                {
                    "success": True,
                    "archive": {
                        "id": meta.get("id"),
                        "name": meta.get("name"),
                        "created_at": meta.get("created_at"),
                    },
                }
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Archive not found")
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to rename chat archive {archive_id}: {exc}")
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    async def update_config_entry(self, request: Request):
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        log_debug(f"{LOG_PREFIX} update_config_entry received payload: {payload}")

        key = str(payload.get("key") or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="Missing configuration key")

        if "value" not in payload:
            log_error(
                f"{LOG_PREFIX} 'value' not in payload. Keys: {list(payload.keys())}"
            )
            log_error(f"{LOG_PREFIX} Full payload: {payload}")
            raise HTTPException(status_code=400, detail="Missing configuration value")

        value = payload.get("value")
        log_debug(
            f"{LOG_PREFIX} Updating config: key={key}, value_type={type(value)}, value_len={len(str(value)) if value else 0}"
        )

        # Get component info before updating
        try:
            definitions = config_registry.export_definitions()
            config_def = next((d for d in definitions if d["key"] == key), None)
            component = config_def.get("component") if config_def else None
        except Exception:
            component = None

        try:
            await config_registry.set_value(key, value, require_persist=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            log_error(f"{LOG_PREFIX} failed to persist config {key}: {exc}")
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            log_error(f"{LOG_PREFIX} failed to update config {key}: {exc}")
            raise HTTPException(
                status_code=500, detail="Failed to update configuration"
            ) from exc

        response_data = {"status": "ok"}

        # Check if component reload is needed. Prefer an explicit flag coming
        # from the config definition (needs_component_reload). This avoids
        # suggesting reloads for synthetic components like 'exposed' unless a
        # variable explicitly declared that changing it requires a reload.
        try:
            needs_reload_flag = (
                bool(config_def.get("needs_component_reload", False))
                if config_def
                else False
            )
        except Exception:
            needs_reload_flag = False

        if component and component not in ["core", "webui"] and needs_reload_flag:
            response_data["requires_reload"] = True
            response_data["component"] = component
            response_data["message"] = (
                f"Configuration updated. Component '{component}' should be reloaded for changes to take effect."
            )
            log_warning(
                f"{LOG_PREFIX} Config '{key}' for component '{component}' changed - component reload recommended"
            )

        return JSONResponse(response_data)

    async def get_logchat_info(self):
        """Return LogChat configuration status."""
        try:
            from core.config import get_log_chat_id, get_log_chat_interface

            log_chat_id = await get_log_chat_id()
            log_chat_interface = await get_log_chat_interface()

            if log_chat_id and log_chat_interface:
                return JSONResponse(
                    {
                        "configured": True,
                        "interface": log_chat_interface,
                        "chat_id": str(log_chat_id),
                    }
                )
            return JSONResponse({"configured": False})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to get logchat info: {exc}")
            return JSONResponse({"configured": False, "error": str(exc)})

    async def get_active_vrm_endpoint(self):
        log_debug(f"{LOG_PREFIX} Getting active VRM: {self.active_vrm}")
        if self.active_vrm:
            # If active_vrm already starts with /, it's already a web URL
            if self.active_vrm.startswith("/"):
                result = {
                    "name": self.active_vrm.split("/")[-1],
                    "url": self.active_vrm,
                }
                log_debug(f"{LOG_PREFIX} Active VRM response (URL path): {result}")
                return JSONResponse(result)

            # Otherwise, build URL from vrm_dir
            vrm_path = self.vrm_dir / self.active_vrm
            # Convert absolute path to web-accessible URL
            try:
                web_path = vrm_path.relative_to(Path(__file__).resolve().parent.parent)
                url = f"/{web_path}"
            except ValueError:
                # Fallback if path is not relative
                url = f"/{self.vrm_dir}/{self.active_vrm}"
            result = {"name": self.active_vrm, "url": url}
            log_debug(f"{LOG_PREFIX} Active VRM response: {result}")
            return JSONResponse(result)
        log_debug(f"{LOG_PREFIX} No active VRM set")
        return JSONResponse({"name": None, "url": None})

    async def set_active_vrm_endpoint(self, request: Request):
        data = await request.json()
        name = data.get("name")
        log_debug(f"{LOG_PREFIX} Request to set active VRM: {name}")
        if not name:
            log_warning(f"{LOG_PREFIX} Set active VRM called without name")
            raise HTTPException(status_code=400, detail="Missing 'name'")
        candidate = self.vrm_dir / Path(name).name
        log_debug(f"{LOG_PREFIX} Checking VRM candidate: {candidate}")
        if not candidate.exists():
            log_error(f"{LOG_PREFIX} VRM not found: {candidate}")
            raise HTTPException(status_code=404, detail="Model not found")
        self._set_active_vrm(candidate.name)
        log_info(f"{LOG_PREFIX} Active VRM set to: {candidate.name}")

        # Broadcast new VRM model to all connected clients
        try:
            if self.animation_handler:
                vrm_url = f"/avatars/{candidate.name}"
                await self.animation_handler.set_vrm_model(vrm_url, candidate.name)
                log_debug(f"{LOG_PREFIX} Broadcast vrm_model: {candidate.name}")
        except Exception as vrm_exc:
            log_warning(f"{LOG_PREFIX} Failed to broadcast vrm_model: {vrm_exc}")

        # Restart idle animation (broadcast to all sessions)
        try:
            if self.persona_manager:
                await self.persona_manager.set_animation_state("idle")
                log_debug(f"{LOG_PREFIX} Started idle animation after VRM switch")
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Error while starting idle animations: {exc}")
        return JSONResponse(
            {
                "status": "ok",
                "name": candidate.name,
                "url": f"/avatars/{candidate.name}",
            }
        )

    async def upload_vrm_model(self, file: UploadFile = File(...)):
        log_info(f"{LOG_PREFIX} ========== VRM UPLOAD START ==========")
        # uploading a custom model means we're no longer using a named skin
        self._current_skin = None
        log_info(
            f"{LOG_PREFIX} VRM upload started: {file.filename if file else 'no file'}"
        )
        log_debug(
            f"{LOG_PREFIX} File content type: {file.content_type if file else 'N/A'}"
        )
        log_debug(
            f"{LOG_PREFIX} File size (from file object): {file.size if hasattr(file, 'size') else 'unknown'}"
        )
        log_debug(f"{LOG_PREFIX} VRM directory: {self.vrm_dir}")
        log_debug(f"{LOG_PREFIX} VRM directory exists: {self.vrm_dir.exists()}")
        log_debug(
            f"{LOG_PREFIX} VRM directory is_dir: {self.vrm_dir.is_dir() if self.vrm_dir.exists() else 'N/A'}"
        )

        if not file or not file.filename:
            log_warning(f"{LOG_PREFIX} VRM upload failed: no file provided")
            raise HTTPException(status_code=400, detail="No file uploaded")

        log_debug(f"{LOG_PREFIX} Original filename: '{file.filename}'")

        if not file.filename.lower().endswith(".vrm"):
            log_warning(
                f"{LOG_PREFIX} VRM upload failed: invalid extension for {file.filename}"
            )
            raise HTTPException(status_code=400, detail="Only .vrm files are accepted")

        filename = self._sanitize_vrm_filename(file.filename)
        log_info(f"{LOG_PREFIX} Sanitized filename: '{filename}'")

        destination = self.vrm_dir / filename
        log_info(f"{LOG_PREFIX} Full destination path: {destination}")
        log_debug(
            f"{LOG_PREFIX} Destination parent exists: {destination.parent.exists()}"
        )
        log_debug(
            f"{LOG_PREFIX} Destination parent is writable: {os.access(destination.parent, os.W_OK) if destination.parent.exists() else 'N/A'}"
        )

        try:
            log_debug(f"{LOG_PREFIX} Opening destination file for writing...")
            # Per new behavior, always write to model.vrm inside the VRM dir (overwrite)
            # and remove any other cached VRMs so only a single model is kept.
            destination = self.vrm_dir / "model.vrm"
            with destination.open("wb") as buffer:
                log_debug(
                    f"{LOG_PREFIX} File opened successfully, starting to read chunks..."
                )
                bytes_written = 0
                chunk_count = 0
                while True:
                    chunk = await file.read(1 << 20)  # 1MB chunks
                    if not chunk:
                        log_debug(f"{LOG_PREFIX} No more chunks to read")
                        break
                    buffer.write(chunk)
                    bytes_written += len(chunk)
                    chunk_count += 1
                    if chunk_count % 5 == 0:  # Log every 5MB
                        log_debug(
                            f"{LOG_PREFIX} Written {bytes_written} bytes so far..."
                        )

                log_info(
                    f"{LOG_PREFIX} VRM upload complete: {filename} ({bytes_written} bytes, {chunk_count} chunks)"
                )
                log_debug(
                    f"{LOG_PREFIX} File exists after write: {destination.exists()}"
                )
                log_debug(
                    f"{LOG_PREFIX} File size on disk: {destination.stat().st_size if destination.exists() else 'N/A'}"
                )

            # cleanup any leftover vrms from previous uploads (only model.vrm should remain)
            try:
                for other in self.vrm_dir.glob("*.vrm"):
                    if other.name != "model.vrm":
                        log_debug(f"{LOG_PREFIX} Removing stale VRM file: {other}")
                        other.unlink()
                log_debug(f"{LOG_PREFIX} VRM cache cleaned, only model.vrm remains")
            except Exception as cleanup_exc:
                log_warning(
                    f"{LOG_PREFIX} Failed to clean up old VRM files: {cleanup_exc}"
                )

            # make the newly uploaded model the active one automatically
            try:
                log_debug(f"{LOG_PREFIX} Setting uploaded VRM as active")
                self._set_active_vrm("model.vrm")
                # broadcast to clients if possible (reuse same logic as set_active_vrm_endpoint)
                if self.animation_handler:
                    vrm_url = "/avatars/model.vrm"
                    await self.animation_handler.set_vrm_model(vrm_url, "model.vrm")
                    log_debug(f"{LOG_PREFIX} Broadcast vrm_model: model.vrm")
                if self.persona_manager:
                    await self.persona_manager.set_animation_state("idle")
                    log_debug(f"{LOG_PREFIX} Started idle animation after VRM upload")
            except Exception as br_exc:
                log_warning(
                    f"{LOG_PREFIX} Failed to broadcast/upload-change events: {br_exc}"
                )

        except Exception as exc:
            log_error(f"{LOG_PREFIX} ⚠️ Failed to store VRM upload: {exc}")
            log_error(f"{LOG_PREFIX} Exception type: {type(exc).__name__}")
            import traceback

            log_error(f"{LOG_PREFIX} Traceback: {traceback.format_exc()}")

            if destination.exists():
                try:
                    destination.unlink()
                    log_debug(f"{LOG_PREFIX} Cleaned up partial upload: {destination}")
                except Exception as cleanup_exc:
                    log_warning(
                        f"{LOG_PREFIX} Failed to cleanup partial VRM upload: {cleanup_exc}"
                    )
            raise HTTPException(
                status_code=500, detail="Failed to store uploaded VRM"
            ) from exc
        finally:
            try:
                await file.close()
            except Exception:
                pass
            log_debug(f"{LOG_PREFIX} File handle closed")

    async def upload_exposed_file(self, key: str, file: UploadFile = File(...)):
        """Upload a file for an exposed variable of type 'file'.

        Saves the uploaded file into: <STORAGE_ROOT>/<key>/<original_filename>
        and updates the exposed variable value to the file's stored path.
        """
        log_info(
            f"{LOG_PREFIX} 📁 Exposed file upload start: key={key}, filename={file.filename if file else 'none'}"
        )
        try:
            from core.variables_engine import exposed_vars

            definition = exposed_vars.get_definition(key)
            if not definition:
                log_warning(
                    f"{LOG_PREFIX} Unknown exposed variable key for file upload: {key}"
                )
                raise HTTPException(status_code=404, detail="Unknown exposed variable")
            if definition.ui_type != "file":
                log_warning(
                    f"{LOG_PREFIX} Exposed variable {key} is not a file type (ui_type={definition.ui_type})"
                )
                raise HTTPException(
                    status_code=400, detail="Variable is not a file type"
                )
            if definition.readonly:
                log_warning(
                    f"{LOG_PREFIX} Attempt to upload to readonly exposed variable {key}"
                )
                raise HTTPException(status_code=403, detail="Variable is read-only")

            if not file or not getattr(file, "filename", None):
                raise HTTPException(status_code=400, detail="No file uploaded")

            # Storage root is configurable via env var; default to /config/storage
            storage_root = Path(
                os.getenv("SYNTH_EXPOSED_STORAGE_ROOT", "/config/storage")
            )
            try:
                storage_root.mkdir(parents=True, exist_ok=True)
            except Exception:
                log_warning(
                    f"{LOG_PREFIX} Could not ensure storage root exists: {storage_root}"
                )

            dest_dir = storage_root / key
            dest_dir.mkdir(parents=True, exist_ok=True)

            # Preserve original filename as requested (use basename to avoid directory components)
            original_name = Path(file.filename).name
            destination = dest_dir / original_name

            log_debug(f"{LOG_PREFIX} Writing exposed file to: {destination}")
            bytes_written = 0
            try:
                with destination.open("wb") as fh:
                    while True:
                        chunk = await file.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        bytes_written += len(chunk)
                # set restrictive permissions
                try:
                    os.chmod(destination, 0o600)
                except Exception:
                    log_debug(
                        f"{LOG_PREFIX} Could not chmod destination: {destination}"
                    )

                stored_path = str(destination.resolve())

                # Persist the file path into the exposed variable so callers can retrieve it
                await exposed_vars.set_value(key, stored_path)

                log_info(
                    f"{LOG_PREFIX} ✅ Exposed file uploaded: {destination} ({bytes_written} bytes)"
                )
                return JSONResponse(
                    {
                        "status": "ok",
                        "stored_path": stored_path,
                        "original_filename": original_name,
                    }
                )
            except Exception as exc:
                log_error(
                    f"{LOG_PREFIX} Failed to write exposed file {destination}: {exc}"
                )
                if destination.exists():
                    try:
                        destination.unlink()
                    except Exception:
                        pass
                raise HTTPException(
                    status_code=500, detail="Failed to store uploaded file"
                ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Unexpected error in upload_exposed_file: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def get_exposed_file(self, key: str):
        """Retrieve the stored file for an exposed variable of type 'file'.

        Serves the file as an attachment using the original filename.
        """
        log_debug(f"{LOG_PREFIX} Request to download exposed file for key: {key}")
        try:
            from core.variables_engine import exposed_vars

            definition = exposed_vars.get_definition(key)
            if not definition or definition.ui_type != "file":
                log_warning(
                    f"{LOG_PREFIX} Download requested for unknown or non-file variable: {key}"
                )
                raise HTTPException(status_code=404, detail="File not found")

            stored = exposed_vars.get_value(key)
            if not stored:
                log_warning(f"{LOG_PREFIX} No file stored for variable: {key}")
                raise HTTPException(
                    status_code=404, detail="No file stored for this variable"
                )

            file_path = Path(str(stored))
            if not file_path.exists() or not file_path.is_file():
                log_warning(
                    f"{LOG_PREFIX} Stored file path missing or invalid: {file_path}"
                )
                raise HTTPException(status_code=404, detail="Stored file not found")

            storage_root = Path(
                os.getenv("SYNTH_EXPOSED_STORAGE_ROOT", "/config/storage")
            ).resolve()
            try:
                # Ensure the file is inside the storage root to avoid path escape
                file_path_resolved = file_path.resolve()
                file_path_resolved.relative_to(storage_root)
            except Exception:
                log_warning(
                    f"{LOG_PREFIX} Stored file path is outside allowed storage: {file_path}"
                )
                raise HTTPException(status_code=403, detail="Access denied")

            headers = {
                "Content-Disposition": f'attachment; filename="{file_path.name}"'
            }
            return FileResponse(
                str(file_path_resolved),
                media_type="application/octet-stream",
                headers=headers,
            )
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Unexpected error in get_exposed_file: {exc}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        log_info(f"{LOG_PREFIX} Setting active VRM to: model.vrm")
        # Persist marker pointing to model.vrm
        try:
            self._set_active_vrm("model.vrm")
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Failed to persist active VRM marker: {exc}")
        log_info(f"{LOG_PREFIX} Active VRM set successfully")

        response_data = {
            "status": "ok",
            "name": "model.vrm",
            "url": "/avatars/model.vrm",
        }
        log_info(f"{LOG_PREFIX} Returning response: {response_data}")
        log_info(f"{LOG_PREFIX} ========== VRM UPLOAD END ==========")

        return JSONResponse(response_data, status_code=201)

    async def delete_vrm_model(self, model_name: str):
        sanitized = Path(model_name).name
        target = self.vrm_dir / sanitized
        if not target.exists():
            raise HTTPException(status_code=404, detail="Model not found")
        try:
            target.unlink()
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to delete VRM {sanitized}: {exc}")
            raise HTTPException(status_code=500, detail="Unable to delete VRM file")

        if self.active_vrm == sanitized:
            fallback = None
            for candidate in sorted(self.vrm_dir.glob("*.vrm")):
                fallback = candidate.name
                break
            self._set_active_vrm(fallback)
        return JSONResponse(self._models_payload())

    async def upload_persona_pack(
        self, file: UploadFile = File(None), folder_path: Optional[str] = None
    ):
        """Upload a persona pack (.zip or .shp) containing a VRM, animations, descriptor and preview image.

        The pack will be extracted into res/synth_webui/personas/<name> and the VRM will be copied into /avatars.
        """
        import zipfile

        personas_dir = (
            Path(__file__).resolve().parent.parent / "res" / "synth_webui" / "personas"
        )
        personas_dir.mkdir(parents=True, exist_ok=True)

        # Two supported modes:
        #  - Uploaded archive (.zip or .shp) via `file`
        #  - Server-side folder copy via `folder_path` (useful for local persona installs)
        dest = None
        temp_path = None
        if folder_path:
            # Treat folder_path as a server-local folder to copy into personas_dir
            src = Path(folder_path).expanduser()
            if not src.exists() or not src.is_dir():
                raise HTTPException(
                    status_code=400,
                    detail="Provided folder_path does not exist or is not a directory",
                )
            # Create a unique dest folder name based on folder basename
            root = src.name
            dest = personas_dir / root
            if dest.exists():
                dest = personas_dir / f"{root}_{uuid.uuid4().hex[:6]}"
            import shutil

            try:
                shutil.copytree(src, dest)
            except Exception as exc:
                log_error(
                    f"{LOG_PREFIX} Failed to copy persona folder from {src} to {dest}: {exc}"
                )
                raise HTTPException(
                    status_code=500, detail="Failed to copy persona folder"
                )
        else:
            if not file or not file.filename:
                raise HTTPException(status_code=400, detail="No file uploaded")

            filename = Path(file.filename).name
            lower = filename.lower()
            if not (lower.endswith(".zip") or lower.endswith(".shp")):
                raise HTTPException(
                    status_code=400,
                    detail="Only .zip or .shp persona packs are accepted",
                )

            # Save uploaded archive to a temp location
            temp_path = personas_dir / f"upload_{uuid.uuid4().hex}.tmp"
            try:
                with temp_path.open("wb") as f:
                    while True:
                        chunk = await file.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
            finally:
                await file.close()

            # Extract
            try:
                with zipfile.ZipFile(temp_path, "r") as zf:
                    # Determine root folder name from archive (or use filename sans ext)
                    root_candidates = [
                        n.split("/")[0] for n in zf.namelist() if n and "/" in n
                    ]
                    root = (
                        root_candidates[0] if root_candidates else Path(filename).stem
                    )
                    dest = personas_dir / root
                    if dest.exists():
                        # create unique folder
                        dest = personas_dir / f"{root}_{uuid.uuid4().hex[:6]}"
                    dest.mkdir(parents=True, exist_ok=True)
                    zf.extractall(dest)

                # Find a .vrm file inside dest
                vrm_file = None
                for p in dest.rglob("*.vrm"):
                    vrm_file = p
                    break

                if vrm_file:
                    # Copy VRM to avatars dir
                    avatars_dir = self.vrm_dir
                    avatars_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = self._sanitize_vrm_filename(vrm_file.name)
                    target = avatars_dir / safe_name
                    import shutil

                    shutil.copy2(vrm_file, target)
                    # Optionally copy animations (we assume animations are relative paths under animations/ in the persona pack)
                    animations_src = dest / "animations"
                    animations_dest = (
                        Path(__file__).resolve().parent.parent
                        / "res"
                        / "synth_webui"
                        / "animations"
                    )
                    if animations_src.exists() and animations_src.is_dir():
                        animations_dest.mkdir(parents=True, exist_ok=True)
                        for anim in animations_src.iterdir():
                            try:
                                shutil.copy2(anim, animations_dest / anim.name)
                            except Exception:
                                pass

                    # If there's a persona metadata file, try to read name/preview
                    meta = None
                    for m in dest.glob("*.md"):
                        try:
                            meta = m.read_text(encoding="utf-8")
                            break
                        except Exception:
                            continue

                    # Mark this VRM as active (optional - for now set as active)
                    self._set_active_vrm(target.name)

                    return JSONResponse(
                        {
                            "status": "ok",
                            "name": target.name,
                            "skin_folder": str(dest),
                            "meta": meta,
                        },
                        status_code=201,
                    )
                else:
                    return JSONResponse(
                        {"status": "error", "detail": "No VRM found in persona pack"},
                        status_code=400,
                    )
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="Invalid zip file")
            except Exception as exc:
                log_error(f"{LOG_PREFIX} Failed to process persona pack: {exc}")
                raise HTTPException(
                    status_code=500, detail="Failed to process persona pack"
                )
            finally:
                try:
                    if temp_path and temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass

    @staticmethod
    def _prettify_name(raw_name: str) -> str:
        if not raw_name:
            return ""
        overrides = {
            "synth_webui": "Synthetic Heart",
            "synth-webui": "Synthetic Heart",
            "synth_webui_interface": "Synthetic Heart",
            "logchat": "LogChat",
        }
        key = str(raw_name)
        lower_key = key.lower()
        if key in overrides:
            return overrides[key]
        if lower_key in overrides:
            return overrides[lower_key]
        cleaned = re.sub(r"[_\-.]+", " ", key).strip()
        if not cleaned:
            return key
        return " ".join(
            part.capitalize() if part.upper() != part else part
            for part in cleaned.split()
        )

    @staticmethod
    def _get_display_name(identifier: str, component: object | None) -> str:
        if component is not None:
            for attr in ("display_name", "friendly_name", "name"):
                value = getattr(component, attr, None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if callable(value):
                    try:
                        result = value()
                    except Exception:  # pragma: no cover - defensive
                        continue
                    if isinstance(result, str) and result.strip():
                        return result.strip()
        return SynthWebUIInterface._prettify_name(identifier)

    @staticmethod
    def _extract_description(component: object) -> str:
        if component is None:
            return ""
        description = ""
        try:
            candidate = getattr(component, "description", None)
            if isinstance(candidate, str):
                description = candidate
            elif callable(candidate):
                result = candidate()
                if isinstance(result, str):
                    description = result
        except Exception:  # pragma: no cover - defensive
            description = ""

        if not description:
            getter = getattr(component, "get_description", None)
            if callable(getter):
                try:
                    result = getter()
                    if isinstance(result, str):
                        description = result
                except Exception:  # pragma: no cover - defensive
                    description = ""

        if not description:
            doc = getattr(component, "__doc__", "") or getattr(
                getattr(component, "__class__", object), "__doc__", ""
            )
            if doc:
                description = doc

        description = (description or "").strip()
        if not description:
            return ""
        # Normalize whitespace to keep UI tidy
        return " ".join(description.split())

    @staticmethod
    def _format_actions(actions) -> List[dict]:
        formatted: List[dict] = []
        if isinstance(actions, dict):
            for name, cfg in actions.items():
                formatted.append(SynthWebUIInterface._format_action_entry(name, cfg))
        elif isinstance(actions, (list, tuple, set)):
            for name in actions:
                formatted.append(
                    {
                        "name": str(name),
                        "description": "",
                        "required_fields": [],
                        "optional_fields": [],
                    }
                )
        return formatted

    @staticmethod
    def _format_action_entry(name: str, config) -> dict:
        entry = {
            "name": str(name),
            "description": "",
            "required_fields": [],
            "optional_fields": [],
        }
        if isinstance(config, dict):
            entry["description"] = str(config.get("description") or "").strip()
            entry["required_fields"] = list(config.get("required_fields") or [])
            entry["optional_fields"] = list(config.get("optional_fields") or [])
        elif isinstance(config, (list, tuple, set)):
            entry["required_fields"] = list(config)
        return entry

    @staticmethod
    def _get_component_meta(name: str) -> dict:
        try:
            from core.core_initializer import core_initializer

            info = core_initializer.components.get(name)  # type: ignore[attr-defined]
            if info:
                status_value = getattr(info.status, "value", str(info.status))
                return {
                    "status": status_value,
                    "details": getattr(info, "details", "") or "",
                    "error": getattr(info, "error", "") or "",
                }

            # Check if it's an interface and if it's disabled
            from core.core_initializer import INTERFACE_REGISTRY

            interface = INTERFACE_REGISTRY.get(name)
            if (
                interface
                and hasattr(interface, "is_enabled")
                and not interface.is_enabled
            ):
                reason = getattr(interface, "disabled_reason", "Disabled")
                return {
                    "status": "disabled",
                    "details": reason,
                    "error": "",
                }
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} meta lookup failed for {name}: {exc}")
        return {"status": "unknown", "details": "", "error": ""}

    async def components_summary(self):
        try:
            from core.core_initializer import (
                PLUGIN_REGISTRY,
                INTERFACE_REGISTRY,
                core_initializer,
            )
            from core.cortex_registry import get_cortex_registry
        except Exception as exc:  # pragma: no cover - defensive
            log_error(f"{LOG_PREFIX} component inspection import failure: {exc}")
            raise HTTPException(
                status_code=500, detail="Unable to inspect components"
            ) from exc

        available_cortexs = []
        try:
            # Derive available cortex kinds from the CortexRegistry metadata to avoid
            # importing wider config helpers (more robust in environments where
            # core.config may transiently fail to import).
            reg = get_cortex_registry()
            available_cortexs = sorted(
                {
                    meta.get("cortex", "llm_provider")
                    for meta in reg._engine_meta.values()
                }
            )
            if not available_cortexs:
                available_cortexs = ["llm_provider"]
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} unable to list available cortex kinds: {exc}")

        try:
            # Resolve active engine via the Base Cortex configuration
            try:
                from core.config import get_active_cortex_engine

                active_engine_temp = await get_active_cortex_engine()
            except Exception:
                active_engine_temp = None

            try:
                reg = get_cortex_registry()
                active_cortex = (
                    reg._engine_meta.get(active_engine_temp, {}).get("cortex")
                    if active_engine_temp
                    else None
                )
            except Exception:
                active_cortex = None
        except Exception as exc:
            log_error(f"{LOG_PREFIX} unable to resolve active cortex: {exc}")
            active_cortex = None

        cortex_reg = get_cortex_registry()
        engine_names = set()
        try:
            if active_cortex:
                engine_names.update(cortex_reg.get_available_engines(active_cortex))
            else:
                engine_names.update(cortex_reg.get_available_engines())
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} unable to list registered engines: {exc}")
        try:
            # Prefer the explicit helper when available; otherwise fall back to
            # enumerating engines from the CortexRegistry to avoid hard failures
            # when core.config cannot be imported (e.g., missing optional deps).
            try:
                from core.config import list_available_cortex_engines

                engine_names.update(list_available_cortex_engines(None))
            except Exception:
                engine_names.update(cortex_reg.get_available_engines())
        except Exception:
            # Keep going even if neither method works
            pass

        try:
            from core.config import get_active_cortex_engine

            active_engine = await get_active_cortex_engine()
        except Exception:
            active_engine = None
        if active_engine:
            engine_names.add(active_engine)

        cortex_engines: List[dict] = []
        for engine_name in sorted(engine_names):
            instance = None
            try:
                instance = cortex_reg.get_engine(engine_name)
            except Exception as exc:
                log_warning(
                    f"{LOG_PREFIX} unable to retrieve engine {engine_name}: {exc}"
                )
            actions = []
            if instance and hasattr(instance, "get_supported_actions"):
                try:
                    actions = self._format_actions(instance.get_supported_actions())
                except Exception as exc:
                    log_warning(
                        f"{LOG_PREFIX} error reading actions for engine {engine_name}: {exc}"
                    )
            elif instance and hasattr(instance, "get_supported_action_types"):
                try:
                    actions = self._format_actions(
                        instance.get_supported_action_types()
                    )
                except Exception as exc:
                    log_warning(
                        f"{LOG_PREFIX} error reading action types for engine {engine_name}: {exc}"
                    )

            meta = self._get_component_meta(engine_name)
            # Login state — only relevant for external-endpoint based engines
            login_state = "unknown"
            logged_in = False
            login_url = ""

            # Gather model information from engines that expose it
            supported_models: list[str] = []
            current_model: str | None = None
            if instance is not None:
                try:
                    if hasattr(instance, "get_supported_models"):
                        supported_models = instance.get_supported_models() or []
                except Exception:
                    pass
                try:
                    if hasattr(instance, "get_current_model"):
                        current_model = instance.get_current_model()
                except Exception:
                    pass

            # Detect external-endpoint engines and pull live endpoint details:
            # the id + extra_config feed the inline per-endpoint config editor,
            # and available_models acts as a fallback when the in-memory bridge
            # predates the first probe.
            try:
                from core.external_endpoints.bridges.cortex_bridge import (
                    ExternalCortexEngine as _ExtCB,
                )

                _is_external = isinstance(instance, _ExtCB)
            except Exception:
                _is_external = False
            endpoint_id: int | None = None
            endpoint_extra_config: dict | None = None
            if _is_external:
                try:
                    from core.external_endpoints.registry import (
                        get_external_endpoint_registry,
                    )

                    _ext_reg = get_external_endpoint_registry()
                    _ep_fresh = await _ext_reg.get_endpoint_by_name(
                        instance._endpoint.name  # type: ignore[union-attr]
                    )
                    if _ep_fresh is not None:
                        endpoint_id = _ep_fresh.id
                        endpoint_extra_config = _ep_fresh.extra_config
                        if not supported_models and _ep_fresh.available_models:
                            supported_models = list(_ep_fresh.available_models)
                            if not current_model and _ep_fresh.default_model:
                                current_model = _ep_fresh.default_model
                except Exception:
                    pass

            # Re-evaluate health dynamically for loaded engines (init-time status
            # may be stale, e.g. API key set after engine was first loaded)
            engine_status = meta["status"]
            engine_details = meta["details"]
            engine_error = meta["error"]
            if instance is not None and hasattr(instance, "get_health_status"):
                try:
                    ok, err_msg = core_initializer._evaluate_cortex_health(instance)
                    if ok:
                        engine_status = "success"
                        engine_details = f"Cortex engine: {instance.__class__.__name__}"
                        engine_error = ""
                    else:
                        engine_status = "failed"
                        engine_error = err_msg or "Engine not ready"
                except Exception:
                    pass

            cortex_engines.append(
                {
                    "name": engine_name,
                    "display_name": self._get_display_name(engine_name, instance),
                    "active": engine_name == active_engine,
                    "loaded": instance is not None,
                    "description": self._extract_description(instance),
                    "label": cortex_reg._engine_meta.get(engine_name, {}).get(
                        "label", ""
                    ),
                    "status": engine_status,
                    "details": engine_details,
                    "error": engine_error,
                    "login_state": login_state,
                    "logged_in": logged_in,
                    "login_url": login_url,
                    "actions": actions,
                    "cortex": cortex_reg._engine_meta.get(engine_name, {}).get(
                        "cortex", "llm_provider"
                    ),
                    "supported_models": supported_models,
                    "current_model": current_model,
                    "is_external": _is_external,
                    "endpoint_id": endpoint_id,
                    "extra_config": endpoint_extra_config,
                }
            )
        interfaces_data: List[dict] = []
        for name, interface in sorted(INTERFACE_REGISTRY.items()):
            description = ""
            if hasattr(interface, "get_interface_instructions"):
                try:
                    description = interface.get_interface_instructions() or ""
                except Exception as exc:
                    log_warning(
                        f"{LOG_PREFIX} interface instruction retrieval failed for {name}: {exc}"
                    )
            if not description:
                description = self._extract_description(interface)

            actions = []
            if hasattr(interface, "get_supported_actions"):
                try:
                    actions = self._format_actions(interface.get_supported_actions())
                except Exception as exc:
                    log_warning(
                        f"{LOG_PREFIX} interface action retrieval failed for {name}: {exc}"
                    )
            elif hasattr(interface, "get_supported_action_types"):
                try:
                    actions = self._format_actions(
                        interface.get_supported_action_types()
                    )
                except Exception as exc:
                    log_warning(
                        f"{LOG_PREFIX} interface action type retrieval failed for {name}: {exc}"
                    )

            meta = self._get_component_meta(name)
            interfaces_data.append(
                {
                    "name": name,
                    "display_name": self._get_display_name(name, interface),
                    "description": description,
                    "actions": actions,
                    "status": meta["status"],
                    "details": meta["details"],
                    "error": meta["error"],
                }
            )

        # Deduplicate interfaces by name to avoid duplicates when modules are scanned multiple times
        seen = set()
        deduped_interfaces = []
        for iface in interfaces_data:
            if iface["name"] in seen:
                log_debug(
                    f"[webui] Skipping duplicate interface entry: {iface['name']}"
                )
                continue
            seen.add(iface["name"])
            deduped_interfaces.append(iface)
        interfaces_data = deduped_interfaces

        plugins_data: List[dict] = []
        for name, plugin in sorted(PLUGIN_REGISTRY.items()):
            description = self._extract_description(plugin)
            actions = []
            if hasattr(plugin, "get_supported_actions"):
                try:
                    actions = self._format_actions(plugin.get_supported_actions())
                except Exception as exc:
                    log_warning(
                        f"{LOG_PREFIX} plugin action retrieval failed for {name}: {exc}"
                    )
            elif hasattr(plugin, "get_supported_action_types"):
                try:
                    actions = self._format_actions(plugin.get_supported_action_types())
                except Exception as exc:
                    log_warning(
                        f"{LOG_PREFIX} plugin action type retrieval failed for {name}: {exc}"
                    )

            meta = self._get_component_meta(name)
            plugins_data.append(
                {
                    "name": name,
                    "display_name": self._get_display_name(name, plugin),
                    "description": description,
                    "actions": actions,
                    "status": meta["status"],
                    "details": meta["details"],
                    "error": meta["error"],
                }
            )

        # Deduplicate plugins by name to avoid duplicates in the UI
        seen_p = set()
        deduped_plugins = []
        for p in plugins_data:
            if p["name"] in seen_p:
                log_debug(f"[webui] Skipping duplicate plugin entry: {p['name']}")
                continue
            seen_p.add(p["name"])
            deduped_plugins.append(p)
        plugins_data = deduped_plugins

        component_summary = {"success": 0, "failed": 0, "loading": 0}
        try:
            for info in core_initializer.components.values():  # type: ignore[attr-defined]
                status = getattr(info.status, "value", str(info.status))
                if status == "success":
                    component_summary["success"] += 1
                elif status == "failed":
                    component_summary["failed"] += 1
                elif status == "loading":
                    component_summary["loading"] += 1
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} unable to compile component summary: {exc}")

        # Check if dev components are enabled
        dev_components_enabled = False
        try:
            dev_components_enabled = core_initializer.are_dev_components_enabled()
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} unable to check dev components status: {exc}")

        # Build a cortex -> engines mapping for the UI (exclude 'live' — shown in its own section)
        by_cortex: dict[str, list[dict]] = {}
        try:
            for e in cortex_engines:
                k = e.get("cortex", "llm_provider")
                by_cortex.setdefault(k, []).append(e)
        except Exception:
            by_cortex = {}

        # Remove 'live' from cortex kinds — it gets its own top-level section
        available_cortexs = [k for k in available_cortexs if k != "live"]

        # Build Vox / Auris / Live engine lists for the dedicated UI sections
        def _caps_desc(caps: dict) -> str:
            active = [k for k, v in (caps or {}).items() if v]
            return ", ".join(active) if active else "none"

        vox_data: list[dict] = []
        try:
            from core.vox_registry import VOX_REGISTRY

            active_vox: str | None = None
            try:
                active_vox = config_registry.get_value("ACTIVE_VOX_ENGINE", None)
            except Exception:
                pass
            # disabled option always available
            vox_data.append(
                {
                    "name": "disabled",
                    "display_name": "Disabled",
                    "label": "No TTS engine (disabled)",
                    "capabilities": {},
                    "description": "TTS disabled",
                    "status": "success",
                    "details": "Active" if active_vox == "disabled" else "",
                    "error": None,
                    "active": active_vox == "disabled",
                }
            )
            for _name in VOX_REGISTRY.get_available_engines():
                _meta = VOX_REGISTRY.get_engine_meta(_name)
                _caps = _meta.get("capabilities") or {}
                _v_available_models: list[str] = []
                _v_default_model: str | None = None
                _v_models_meta: list[dict] = []
                _v_instance = VOX_REGISTRY._instances.get(_name)
                if _v_instance is not None and hasattr(_v_instance, "_endpoint"):
                    _v_ep = _v_instance._endpoint
                    _v_available_models = list(
                        getattr(_v_ep, "available_models", None) or []
                    )
                    _v_default_model = getattr(_v_ep, "default_model", None)
                    _v_models_meta = list(getattr(_v_ep, "models_metadata", None) or [])
                vox_data.append(
                    {
                        "name": _name,
                        "display_name": _name.replace("_", " ").title(),
                        "label": _meta.get("label", ""),
                        "capabilities": _caps,
                        "description": f"TTS engine — capabilities: {_caps_desc(_caps)}",
                        "status": "success",
                        "details": "Active" if _name == active_vox else "",
                        "error": None,
                        "active": _name == active_vox,
                        "available_models": _v_available_models,
                        "default_model": _v_default_model,
                        "models_meta": _v_models_meta,
                    }
                )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} unable to build Vox engine list: {exc}")

        auris_data: list[dict] = []
        try:
            from core.auris_registry import AURIS_REGISTRY

            active_auris: str | None = None
            try:
                active_auris = config_registry.get_value(
                    "ACTIVE_AURIS_ENGINE",
                    None,
                    label="Active Auris Engine",
                    description="Name of the active Auris speech-to-text engine. Set to 'disabled' to turn off the Auris subsystem.",
                    component="auris_plugin",
                    group="plugins",
                )
            except Exception:
                pass
            # add disabled option first
            auris_data.append(
                {
                    "name": "disabled",
                    "display_name": "Disabled",
                    "label": "No STT engine (disabled)",
                    "capabilities": {},
                    "description": "STT disabled",
                    "status": "success",
                    "details": "Active" if active_auris == "disabled" else "",
                    "error": None,
                    "active": active_auris == "disabled",
                }
            )
            # Hardcoded pseudo-engine: bypass Auris transcription entirely and
            # forward the raw audio bytes inline to the Cortex engine so an
            # audio-capable multimodal model can hear the audio directly.  Only
            # useful when the active Cortex endpoint accepts inline audio.
            auris_data.append(
                {
                    "name": "inline",
                    "display_name": "Inline (send to LLM)",
                    "label": "Forward audio inline to a multimodal Cortex engine",
                    "capabilities": {"audio": True},
                    "description": (
                        "Bypass Auris transcription and send the audio directly "
                        "to the LLM (requires an audio-capable Cortex endpoint)"
                    ),
                    "status": "success",
                    "details": "Active" if active_auris == "inline" else "",
                    "error": None,
                    "active": active_auris == "inline",
                }
            )
            for _name in AURIS_REGISTRY.get_available_engines():
                _meta = AURIS_REGISTRY.get_engine_meta(_name)
                _caps = _meta.get("capabilities") or {}
                _a_available_models: list[str] = []
                _a_default_model: str | None = None
                _a_models_meta: list[dict] = []
                _a_instance = AURIS_REGISTRY._instances.get(_name)
                if _a_instance is not None and hasattr(_a_instance, "_endpoint"):
                    _a_ep = _a_instance._endpoint
                    _a_available_models = list(
                        getattr(_a_ep, "available_models", None) or []
                    )
                    _a_default_model = getattr(_a_ep, "default_model", None)
                    _a_models_meta = list(getattr(_a_ep, "models_metadata", None) or [])
                auris_data.append(
                    {
                        "name": _name,
                        "display_name": _name.replace("_", " ").title(),
                        "label": _meta.get("label", ""),
                        "capabilities": _caps,
                        "description": f"STT engine — capabilities: {_caps_desc(_caps)}",
                        "status": "success",
                        "details": "Active" if _name == active_auris else "",
                        "error": None,
                        "active": _name == active_auris,
                        "available_models": _a_available_models,
                        "default_model": _a_default_model,
                        "models_meta": _a_models_meta,
                    }
                )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} unable to build Auris engine list: {exc}")

        # Live section: cortex engines with kind='live' + LIVE_REGISTRY engines
        # determine which live engine (if any) is currently marked active
        active_live: str | None = None
        try:
            # we persist the selected live engine using the LIVE_CORTEX key
            active_live = config_registry.get_value("LIVE_CORTEX", None)
        except Exception:  # pragma: no cover - defensive
            active_live = None

        live_data: list[dict] = list(by_cortex.get("live", []))
        # Fix active flag for cortex live engines (they were marked using BASE_CORTEX's active_engine)
        for engine in live_data:
            engine["active"] = engine.get("name") == active_live
        # always offer disabled choice; mark it active if the config says so
        live_data.insert(
            0,
            {
                "name": "disabled",
                "display_name": "Disabled",
                "label": "No live engine (disabled)",
                "capabilities": {},
                "description": "Live subsystem turned off",
                "status": "success",
                "details": "Active" if active_live == "disabled" else "",
                "error": None,
                "active": active_live == "disabled",
            },
        )
        try:
            from core.live_registry import LIVE_REGISTRY

            existing_names = {e["name"] for e in live_data}
            for _name in LIVE_REGISTRY.get_available_engines():
                if _name in existing_names:
                    continue
                _meta = LIVE_REGISTRY.get_engine_meta(_name)
                _caps = _meta.get("capabilities") or {}
                live_data.append(
                    {
                        "name": _name,
                        "display_name": _name.replace("_", " ").title(),
                        "label": _meta.get("label", ""),
                        "capabilities": _caps,
                        "description": f"Live streaming engine — capabilities: {_caps_desc(_caps)}",
                        "status": "success",
                        "details": "Active" if _name == active_live else "",
                        "error": None,
                        "active": _name == active_live,
                    }
                )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} unable to build Live engine list: {exc}")

        iris_data: list[dict] = []
        try:
            from core.iris_registry import IRIS_REGISTRY

            active_iris: str | None = None
            try:
                active_iris = config_registry.get_value(
                    "ACTIVE_IRIS_ENGINE",
                    None,
                    label="Active Iris Engine",
                    description="Name of the active Iris vision engine. Set to 'disabled' to turn off the Iris subsystem.",
                    component="iris_plugin",
                    group="plugins",
                )
            except Exception:
                pass
            iris_data.append(
                {
                    "name": "disabled",
                    "display_name": "Disabled",
                    "label": "No vision engine (disabled)",
                    "capabilities": {},
                    "description": "Vision disabled",
                    "status": "success",
                    "details": "Active" if active_iris == "disabled" else "",
                    "error": None,
                    "active": active_iris == "disabled",
                }
            )
            # Hardcoded pseudo-engine: bypass Iris entirely and forward image /
            # video bytes inline to the Cortex engine so a vision-capable LLM can
            # see the media directly (no separate description step).  Only takes
            # effect when the active Cortex endpoint is marked vision-capable.
            iris_data.append(
                {
                    "name": "inline",
                    "display_name": "Inline (send to LLM)",
                    "label": "Forward images inline to a multimodal Cortex engine",
                    "capabilities": {"vision": True},
                    "description": (
                        "Bypass Iris descriptions and send the image directly to "
                        "the LLM (requires a vision-capable Cortex endpoint)"
                    ),
                    "status": "success",
                    "details": "Active" if active_iris == "inline" else "",
                    "error": None,
                    "active": active_iris == "inline",
                }
            )
            for _name in IRIS_REGISTRY.get_available_engines():
                _meta = IRIS_REGISTRY.get_engine_meta(_name)
                _caps = _meta.get("capabilities") or {}
                _available_models: list[str] = []
                _default_model: str | None = None
                _models_meta: list[dict] = []
                _instance = IRIS_REGISTRY.get_instance(_name)
                if _instance is not None and hasattr(_instance, "_endpoint"):
                    _ep = _instance._endpoint
                    _available_models = list(
                        getattr(_ep, "available_models", None) or []
                    )
                    _default_model = getattr(_ep, "default_model", None)
                    _models_meta = list(getattr(_ep, "models_metadata", None) or [])
                iris_data.append(
                    {
                        "name": _name,
                        "display_name": _name.replace("_", " ").title(),
                        "label": _meta.get("label", ""),
                        "capabilities": _caps,
                        "description": f"Vision engine — capabilities: {_caps_desc(_caps)}",
                        "status": "success",
                        "details": "Active" if _name == active_iris else "",
                        "error": None,
                        "active": _name == active_iris,
                        "available_models": _available_models,
                        "default_model": _default_model,
                        "models_meta": _models_meta,
                    }
                )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} unable to build Iris engine list: {exc}")

        # Build scope overrides for the UI (Grillo, Trainer, Live cortex selectors)
        # Single source of truth: derive options from the same data already built above.
        cortex_scopes: list[dict] = []
        try:
            # Grillo/Trainer: only llm_provider engines — same source as the main
            # engine selector in the Engines tab (by_cortex is already built above).
            llm_engines_sorted = sorted(
                e["name"] for e in by_cortex.get("llm_provider", [])
            )
            # Live scope: LIVE_REGISTRY is the authoritative source for streaming
            # engines; fall back to CortexRegistry if the registry is unavailable.
            live_engine_names: list[str] = ["disabled"]
            try:
                from core.live_registry import LIVE_REGISTRY as _LIVE_REG

                live_engine_names += sorted(_LIVE_REG.get_available_engines())
            except Exception:
                try:
                    live_engine_names += sorted(
                        cortex_reg.get_engines_by_cortex("live")
                    )
                except Exception:
                    pass
            cortex_scopes = [
                {
                    "key": "GRILLO_CORTEX",
                    "label": "Grillo",
                    "value": config_registry.get_value("GRILLO_CORTEX", "Default"),
                    "options": ["Default"] + llm_engines_sorted,
                },
                {
                    "key": "TRAINER_CORTEX",
                    "label": "Trainer",
                    "value": config_registry.get_value("TRAINER_CORTEX", "Default"),
                    "options": ["Default"] + llm_engines_sorted,
                },
                {
                    "key": "LIVE_CORTEX",
                    "label": "Live",
                    "value": config_registry.get_value("LIVE_CORTEX", "Default"),
                    "options": ["Default"] + live_engine_names,
                },
            ]
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} unable to build cortex scopes: {exc}")

        payload = {
            "cortex": {
                "available_kinds": available_cortexs,
                "active_kind": active_cortex,
                "active_engine": active_engine,
                "engines": cortex_engines,
                "by_cortex": by_cortex,
                "scopes": cortex_scopes,
            },
            "vox": vox_data,
            "auris": auris_data,
            "iris": iris_data,
            "iris_current_model": (
                config_registry.get_value("IRIS_DEFAULT_MODEL", "") or ""
            ),
            "vox_current_model": (
                config_registry.get_value("VOX_DEFAULT_MODEL", "") or ""
            ),
            "auris_current_model": (
                config_registry.get_value("AURIS_DEFAULT_MODEL", "") or ""
            ),
            "live": live_data,
            "interfaces": interfaces_data,
            "plugins": plugins_data,
            "summary": component_summary,
            "dev_components_enabled": dev_components_enabled,
        }
        return JSONResponse(payload)

    async def set_cortex_engine(self, request: Request):
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        name = str(data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Missing 'name'")

        try:
            from core.config import switch_active_cortex_engine as switch_engine
        except Exception as exc:  # pragma: no cover - defensive
            log_error(f"{LOG_PREFIX} unable to import engine switching helpers: {exc}")
            raise HTTPException(
                status_code=500, detail="Unable to access engine configuration"
            ) from exc

        try:
            # Use the centralized switch function with hot-swap
            await switch_engine(name, use_hot_swap=True)
            log_info(f"{LOG_PREFIX} Successfully switched active engine to {name}")
        except ValueError as exc:
            log_warning(f"{LOG_PREFIX} Engine not available: {exc}")
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            # Log and return a useful error message to help debugging switch failures
            log_error(f"{LOG_PREFIX} failed to switch active engine to {name}: {exc}")
            # Include exception message in the HTTP response for easier debugging in dev environments
            raise HTTPException(
                status_code=500, detail=f"Failed to activate engine '{name}': {exc}"
            ) from exc

        return JSONResponse({"status": "ok", "active": name})

    async def set_cortex_model(self, request: Request) -> JSONResponse:
        """Set the active model for the currently loaded cortex engine.

        Expects JSON: ``{"engine": "openrouter", "model": "anthropic/claude-sonnet-4"}``
        """
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        engine_name = str(data.get("engine") or "").strip()
        model_name = str(data.get("model") or "").strip()
        if not engine_name or not model_name:
            raise HTTPException(status_code=400, detail="Missing 'engine' or 'model'")

        try:
            from core.cortex_registry import get_cortex_registry

            registry = get_cortex_registry()
            instance = registry.get_engine(engine_name)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} unable to access Cortex registry: {exc}")
            raise HTTPException(
                status_code=500, detail="Unable to access Cortex registry"
            ) from exc

        if instance is None:
            # if the requested name corresponds to a live engine, we treat it as a
            # no-op; live engines manage their own models separately and are not
            # part of the Cortex registry.  Returning success avoids a confusing
            # 404 and keeps the dropdown from resetting to "disabled".
            from core.live_registry import LIVE_REGISTRY

            if engine_name in LIVE_REGISTRY.get_available_engines():
                log_info(
                    f"{LOG_PREFIX} ignoring model set for live engine '{engine_name}'"
                )
                return JSONResponse(
                    {"status": "ok", "engine": engine_name, "model": model_name}
                )

            raise HTTPException(
                status_code=404, detail=f"Engine '{engine_name}' not loaded"
            )

        if not hasattr(instance, "set_current_model"):
            raise HTTPException(
                status_code=400,
                detail=f"Engine '{engine_name}' does not support model selection",
            )

        try:
            instance.set_current_model(model_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            log_error(
                f"{LOG_PREFIX} failed to set model '{model_name}' on {engine_name}: {exc}"
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to set model: {exc}",
            ) from exc

        # Also persist to the relevant config key if available
        try:
            model_config_keys = {
                "openrouter": "OPENROUTER_DEFAULT_MODEL",
                "gemini_api": "GEMINI_MODEL",
                "openapi": "OPENAPI_DEFAULT_MODEL",
            }
            config_key = model_config_keys.get(engine_name)
            if config_key:
                await config_registry.set_value(config_key, model_name)
        except Exception as exc:
            log_warning(
                f"{LOG_PREFIX} model set on engine but config persist failed: {exc}"
            )

        # Persist model selection to the DB for external endpoint engines
        try:
            from core.external_endpoints.bridges.cortex_bridge import (
                ExternalCortexEngine,
            )

            if isinstance(instance, ExternalCortexEngine):
                from core.external_endpoints.registry import (
                    get_external_endpoint_registry,
                )

                await get_external_endpoint_registry().set_default_model(
                    instance._endpoint.id, model_name
                )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} ext endpoint model DB persist failed: {exc}")

        log_info(f"{LOG_PREFIX} Model for '{engine_name}' set to '{model_name}'")
        return JSONResponse(
            {"status": "ok", "engine": engine_name, "model": model_name}
        )

    async def _set_external_media_model(
        self,
        request: Request,
        registry_getter,
        extra_config_key: str,
        subsystem: str,
    ) -> JSONResponse:
        """Persist a model selection for an external Vox/Auris engine.

        The bridge reads the chosen model from the endpoint's
        ``extra_config`` (``tts_model`` for Vox, ``stt_model`` for Auris) with
        a fallback to ``default_model``. We therefore merge the value into the
        endpoint's ``extra_config`` and let ``update_endpoint`` re-sync the
        live registry so a running instance picks up the change without a
        restart.
        """
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        engine_name = str(data.get("engine") or "").strip()
        model_name = str(data.get("model") or "").strip()
        if not engine_name or not model_name:
            raise HTTPException(status_code=400, detail="Missing 'engine' or 'model'")

        try:
            registry = registry_getter()
            instance = registry.load_engine(engine_name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            log_error(f"{LOG_PREFIX} unable to access {subsystem} registry: {exc}")
            raise HTTPException(
                status_code=500, detail=f"Unable to access {subsystem} registry"
            ) from exc

        endpoint = (
            getattr(instance, "_endpoint", None) if instance is not None else None
        )
        if endpoint is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Engine '{engine_name}' is not an external endpoint engine "
                    f"and does not support model selection"
                ),
            )

        try:
            from core.external_endpoints.registry import (
                get_external_endpoint_registry,
            )

            ep_registry = get_external_endpoint_registry()
            merged = dict(endpoint.extra_config or {})
            merged[extra_config_key] = model_name
            await ep_registry.update_endpoint(endpoint.id, extra_config=merged)
        except HTTPException:
            raise
        except Exception as exc:
            log_error(
                f"{LOG_PREFIX} failed to set {subsystem} model '{model_name}' "
                f"on '{engine_name}': {exc}"
            )
            raise HTTPException(
                status_code=500, detail=f"Failed to set model: {exc}"
            ) from exc

        log_info(
            f"{LOG_PREFIX} {subsystem} model for '{engine_name}' set to '{model_name}'"
        )
        return JSONResponse(
            {"status": "ok", "engine": engine_name, "model": model_name}
        )

    async def set_vox_model(self, request: Request) -> JSONResponse:
        """Set the active TTS model for an external Vox engine."""
        from core.vox_registry import VOX_REGISTRY

        return await self._set_external_media_model(
            request, lambda: VOX_REGISTRY, "tts_model", "Vox"
        )

    async def set_auris_model(self, request: Request) -> JSONResponse:
        """Set the active STT model for an external Auris engine."""
        from core.auris_registry import AURIS_REGISTRY

        return await self._set_external_media_model(
            request, lambda: AURIS_REGISTRY, "stt_model", "Auris"
        )

    async def cortex_login(self, request: Request):
        """Selenium-based login is no longer supported.

        The embedded Selenium engine has been removed. Use the external
        selenium-llm-engine service and configure it as an external endpoint.
        """
        raise HTTPException(
            status_code=422,
            detail=(
                "Selenium-based login is no longer supported. "
                "Use the external selenium-llm-engine endpoint."
            ),
        )

    # Cortex endpoints

    async def run_component(self, request: Request):
        """Run a component/plugin action on demand.

        Expected JSON: { "name": "component_name", "action": "action_name", "payload": { ... } }
        """
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        name = str(data.get("name") or data.get("component") or "").strip()
        action = str(data.get("action") or "run_now")
        payload = data.get("payload") or {}

        if not name:
            raise HTTPException(
                status_code=400, detail="Missing 'name' / 'component' field"
            )

        try:
            from core.core_initializer import PLUGIN_REGISTRY, INTERFACE_REGISTRY
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Unable to access registries: {exc}")
            raise HTTPException(
                status_code=500, detail="Unable to access component registry"
            ) from exc

        component = PLUGIN_REGISTRY.get(name) or INTERFACE_REGISTRY.get(name)
        if component is None:
            raise HTTPException(status_code=404, detail=f"Component '{name}' not found")

        # Prefer standardized run_action method
        if hasattr(component, "run_action"):
            try:
                result = component.run_action(
                    action, payload, context=data.get("context")
                )
                if asyncio.iscoroutine(result):
                    result = await result
                return JSONResponse({"status": "ok", "result": result})
            except Exception as exc:
                log_error(f"{LOG_PREFIX} Component run_action failed for {name}: {exc}")
                raise HTTPException(
                    status_code=500, detail=f"Component run_action failed: {exc}"
                ) from exc

        # Fallback to common method names
        for method_name in ("run_now", "run_once", "execute_now"):
            if hasattr(component, method_name):
                try:
                    meth = getattr(component, method_name)
                    result = (
                        meth(payload)
                        if not asyncio.iscoroutinefunction(meth)
                        else await meth(payload)
                    )
                    return JSONResponse({"status": "ok", "result": result})
                except Exception as exc:
                    log_error(
                        f"{LOG_PREFIX} Component {method_name} failed for {name}: {exc}"
                    )
                    raise HTTPException(
                        status_code=500, detail=f"Component {method_name} failed: {exc}"
                    ) from exc

        raise HTTPException(
            status_code=400, detail="Component does not support run_action or run_now"
        )

    async def reload_component(self, request: Request):
        """Reload a specific component (interface or plugin)."""
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        component_type = str(data.get("type") or "").strip().lower()
        component_name = str(data.get("name") or "").strip()

        if not component_type or component_type not in [
            "interface",
            "plugin",
            "cortex",
        ]:
            raise HTTPException(
                status_code=400,
                detail="Missing or invalid 'type'. Must be 'interface', 'plugin', or 'cortex'",
            )

        if not component_name:
            raise HTTPException(status_code=400, detail="Missing 'name'")

        try:
            from core.core_initializer import PLUGIN_REGISTRY, INTERFACE_REGISTRY
            from core.cortex_registry import get_cortex_registry
        except Exception as exc:
            log_error(f"{LOG_PREFIX} unable to import registries: {exc}")
            raise HTTPException(
                status_code=500, detail="Unable to access component registries"
            ) from exc

        try:
            if component_type == "interface":
                # Reload interface
                interface_instance = INTERFACE_REGISTRY.get(component_name)
                if not interface_instance:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Interface '{component_name}' not found",
                    )

                # Stop if running
                if hasattr(interface_instance, "stop"):
                    log_info(f"{LOG_PREFIX} Stopping interface '{component_name}'...")
                    try:
                        await interface_instance.stop()
                    except Exception as stop_exc:
                        log_warning(
                            f"{LOG_PREFIX} Error stopping interface '{component_name}': {stop_exc}"
                        )

                # Start again
                if hasattr(interface_instance, "start"):
                    log_info(f"{LOG_PREFIX} Starting interface '{component_name}'...")
                    await interface_instance.start()
                else:
                    log_warning(
                        f"{LOG_PREFIX} Interface '{component_name}' has no start() method"
                    )

                log_info(
                    f"{LOG_PREFIX} Interface '{component_name}' reloaded successfully"
                )
                return JSONResponse(
                    {
                        "status": "ok",
                        "message": f"Interface '{component_name}' reloaded successfully",
                    }
                )

            elif component_type == "plugin":
                # Reload plugin
                plugin_instance = PLUGIN_REGISTRY.get(component_name)
                if not plugin_instance:
                    raise HTTPException(
                        status_code=404, detail=f"Plugin '{component_name}' not found"
                    )

                # Plugins typically don't need reload, but we can report success
                log_info(
                    f"{LOG_PREFIX} Plugin '{component_name}' noted for reload (plugins use ConfigVar auto-updates)"
                )
                return JSONResponse(
                    {
                        "status": "ok",
                        "message": f"Plugin '{component_name}' configuration updated",
                    }
                )

            elif component_type == "cortex":
                # Reload cortex engine
                cortex_registry = get_cortex_registry()

                # Check if engine exists
                if component_name not in cortex_registry.get_available_engines():
                    raise HTTPException(
                        status_code=404, detail=f"Engine '{component_name}' not found"
                    )

                # Unload current instance if exists
                current_instance = cortex_registry.get_engine(component_name)
                if current_instance:
                    log_info(f"{LOG_PREFIX} Unloading engine '{component_name}'...")
                    cortex_registry.unload_engine(component_name)

                # Reload the engine
                log_info(f"{LOG_PREFIX} Reloading engine '{component_name}'...")
                try:
                    cortex_registry.load_engine(component_name)
                    log_info(
                        f"{LOG_PREFIX} Cortex engine '{component_name}' reloaded successfully"
                    )
                    return JSONResponse(
                        {
                            "status": "ok",
                            "message": f"Cortex engine '{component_name}' reloaded successfully",
                        }
                    )
                except Exception as load_exc:
                    log_error(
                        f"{LOG_PREFIX} Failed to reload Cortex engine '{component_name}': {load_exc}"
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to reload Cortex engine '{component_name}': {str(load_exc)}",
                    ) from load_exc

        except HTTPException:
            raise
        except Exception as exc:
            log_error(
                f"{LOG_PREFIX} failed to reload {component_type} '{component_name}': {exc}"
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to reload {component_type} '{component_name}': {str(exc)}",
            ) from exc

    async def toggle_dev_components(self, request: Request):
        """Enable or disable dev components discovery (runtime only, not persistent)."""
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        enabled = data.get("enabled", False)

        try:
            from core.core_initializer import core_initializer
            import main

            # Set the flag in both core_initializer AND main.py (so it persists across restart)
            core_initializer.enable_dev_components(enabled)
            main.set_dev_components_enabled(enabled)

            status_msg = "enabled" if enabled else "disabled"
            log_info(
                f"{LOG_PREFIX} Dev components {status_msg} globally (will persist across restarts)"
            )

            # Note: This does NOT automatically reload components - user must restart
            return JSONResponse(
                {
                    "status": "ok",
                    "enabled": enabled,
                    "message": f"Dev components {status_msg}. Restart required to apply changes.",
                }
            )

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to toggle dev components: {exc}")
            raise HTTPException(
                status_code=500, detail=f"Failed to toggle dev components: {str(exc)}"
            ) from exc

    async def restart_system(self, request: Request):
        """Restart the entire SyntH system by triggering the restart mechanism."""
        try:
            log_info(f"{LOG_PREFIX} System restart requested via API")

            # Send response before restarting
            response = JSONResponse(
                {
                    "status": "ok",
                    "message": "SyntH is restarting... This may take a few moments.",
                }
            )

            # Schedule restart after response is sent
            import asyncio

            async def do_restart():
                await asyncio.sleep(1)  # Give time for response to be sent
                log_info(f"{LOG_PREFIX} Triggering system restart...")

                # Import and call the restart function from main
                try:
                    import main

                    main.request_restart()
                except Exception as e:
                    log_error(f"{LOG_PREFIX} Failed to trigger restart: {e}")

            asyncio.create_task(do_restart())

            return response

        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to restart system: {exc}")
            raise HTTPException(
                status_code=500, detail=f"Failed to restart system: {str(exc)}"
            ) from exc

    def start_server_async(self) -> None:
        """Start the web server as an asyncio task. Call this from the main event loop."""
        try:
            if not hasattr(self, "_server_task") or (
                hasattr(self, "_server_task")
                and (self._server_task is None or self._server_task.done())
            ):
                log_info(
                    f"{LOG_PREFIX} Starting {BRAND_NAME} server as asyncio task on http://{self.host}:{self.port}"
                )
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                    self._server_task = loop.create_task(self._run_server())
                    log_info(
                        f"{LOG_PREFIX} Server task scheduled on running loop: {loop}"
                    )
                except RuntimeError:
                    # No running loop - fallback to create_task which may raise later
                    log_warning(
                        f"{LOG_PREFIX} No running event loop found when scheduling server task; attempting asyncio.create_task fallback"
                    )
                    self._server_task = asyncio.create_task(self._run_server())
            else:
                log_info(f"{LOG_PREFIX} Server task already running")
        except Exception as exc:
            import traceback

            log_error(f"{LOG_PREFIX} Exception while scheduling server task: {exc}")
            log_error(f"{LOG_PREFIX} Traceback: {traceback.format_exc()}")

    async def _run_server(self) -> None:
        """Create and run the uvicorn server."""
        import uvicorn

        try:
            scheme = "https" if self.tls_enabled else "http"
            log_info(
                f"{LOG_PREFIX} Creating Uvicorn config for {scheme}://{self.host}:{self.port}"
            )
            # If TLS is enabled, ensure certs exist (or generate self-signed)
            if self.tls_enabled:
                try:
                    self._ensure_tls_files()
                except Exception as tls_exc:
                    log_warning(
                        f"{LOG_PREFIX} TLS requested but failed to prepare certificates: {tls_exc}"
                    )
                    self.tls_enabled = False
            config_kwargs: dict[str, Any] = dict(
                app=self.app,
                host=self.host,
                port=self.port,
                log_level=self.log_level or "info",
                lifespan="off",
                # Uvicorn's default (None) waits indefinitely for open
                # connections to close on SIGINT before server.serve()
                # returns. The stage keeps long-lived WebSockets open
                # (karada state broadcast, mic streaming for barge-in) that
                # don't close promptly, which blocks serve() forever — and
                # until it returns, uvicorn never hands SIGINT back to
                # main.py's own shutdown handler, so the whole app hangs.
                # Bound it so one Ctrl+C is enough.
                timeout_graceful_shutdown=5,
            )
            if self.tls_enabled and self.tls_certfile and self.tls_keyfile:
                log_info(
                    f"{LOG_PREFIX} TLS enabled, using certfile={self.tls_certfile} keyfile={self.tls_keyfile}"
                )
                config_kwargs.update(
                    {
                        "ssl_certfile": self.tls_certfile,
                        "ssl_keyfile": self.tls_keyfile,
                    }
                )
            # If TLS is enabled and an HTTP port is provided, start both HTTPS and HTTP servers
            if self.tls_enabled and self.http_port and self.http_port != self.port:
                # HTTPS server (original)
                config_https = uvicorn.Config(**config_kwargs)
                server_https = uvicorn.Server(config_https)
                # HTTP server (no TLS)
                config_http_kwargs = dict(**config_kwargs)
                config_http_kwargs.update({"port": self.http_port})
                # Remove SSL keys for HTTP server
                config_http_kwargs.pop("ssl_certfile", None)
                config_http_kwargs.pop("ssl_keyfile", None)
                config_http = uvicorn.Config(**config_http_kwargs)
                server_http = uvicorn.Server(config_http)

                with self._server_lock:
                    self._server = server_https

                log_info(
                    f"{LOG_PREFIX} Starting Uvicorn HTTPS server on {self.host}:{self.port} and HTTP server on {self.host}:{self.http_port}..."
                )

                # Run both servers concurrently
                https_task = asyncio.create_task(server_https.serve())
                http_task = asyncio.create_task(server_http.serve())

                await asyncio.gather(https_task, http_task)
                log_info(f"{LOG_PREFIX} Uvicorn servers stopped normally")
            else:
                config = uvicorn.Config(**config_kwargs)
                server = uvicorn.Server(config)
                with self._server_lock:
                    self._server = server
                log_info(f"{LOG_PREFIX} Starting Uvicorn server...")
                await server.serve()
                log_info(f"{LOG_PREFIX} Uvicorn server stopped normally")
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Error in _run_server: {exc}")
            import traceback

            log_error(f"{LOG_PREFIX} Traceback: {traceback.format_exc()}")
        finally:
            with self._server_lock:
                self._server = None
            log_info(f"{LOG_PREFIX} _run_server cleanup completed")

    def cleanup(self) -> None:
        with self._server_lock:
            server = self._server
        if server is not None:
            try:
                server.should_exit = True
            except Exception:  # pragma: no cover - defensive
                pass
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=2)

    def _ensure_tls_files(self) -> None:
        """Ensure TLS certificate and key files exist, generating self-signed files if needed.

        The generated files are stored under a default cert dir, by default `/config/ssl`.
        This function will update `self.tls_certfile` and `self.tls_keyfile` to point to
        the created or provided files.
        """
        if not self.tls_enabled:
            return

        cert_dir = os.getenv("SYNTH_WEBUI_CERT_DIR", "/config/ssl")
        try:
            Path(cert_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log_warning(f"{LOG_PREFIX} Could not create cert dir {cert_dir}: {e}")

        if self.tls_certfile and self.tls_keyfile:
            if Path(self.tls_certfile).exists() and Path(self.tls_keyfile).exists():
                return
            else:
                log_warning(
                    f"{LOG_PREFIX} Provided TLS files not found: cert={self.tls_certfile}, key={self.tls_keyfile}"
                )

        # Use default filenames if not specified
        default_cert = os.getenv(
            "SYNTH_WEBUI_CERTFILE", os.path.join(cert_dir, "synth_webui.crt")
        )
        default_key = os.getenv(
            "SYNTH_WEBUI_KEYFILE", os.path.join(cert_dir, "synth_webui.key")
        )

        # if both exist, use them
        if Path(default_cert).exists() and Path(default_key).exists():
            self.tls_certfile = default_cert
            self.tls_keyfile = default_key
            return

        # Check for image-seeded default certs (built into the image during Dockerfile build)
        baked_cert = os.path.join(
            os.path.dirname(__file__), "..", "res", "default_ssl", "synth_webui.crt"
        )
        baked_key = os.path.join(
            os.path.dirname(__file__), "..", "res", "default_ssl", "synth_webui.key"
        )
        try:
            baked_cert = os.path.abspath(baked_cert)
            baked_key = os.path.abspath(baked_key)
            if Path(baked_cert).exists() and Path(baked_key).exists():
                # Copy baked certs into cert_dir so they can be replaced by runtime-generated certs if needed
                import shutil

                shutil.copyfile(baked_cert, default_cert)
                shutil.copyfile(baked_key, default_key)
                self.tls_certfile = default_cert
                self.tls_keyfile = default_key
                log_info(
                    f"{LOG_PREFIX} Seeded TLS certs copied from image defaults to {cert_dir}"
                )
                return
        except Exception:
            # Non-fatal; proceed to generation below
            pass

        # Attempt to generate a self-signed cert using openssl if available
        import shutil
        import subprocess

        openssl_path = shutil.which("openssl")
        if not openssl_path:
            # If openssl is not available, try to use Python's cryptography package
            try:
                from cryptography import x509  # type: ignore

                has_cryptography = True
            except Exception:
                has_cryptography = False

            if not has_cryptography:
                raise RuntimeError(
                    "Cannot generate self-signed certificate: openssl not found and cryptography not installed"
                )

            # Use cryptography to generate self-signed cert
            try:
                from cryptography.hazmat.primitives import serialization, hashes
                from cryptography.hazmat.primitives.asymmetric import rsa
                from cryptography.x509.oid import NameOID
                import datetime

                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                subject = issuer = x509.Name(
                    [
                        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
                        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "NA"),
                        x509.NameAttribute(NameOID.LOCALITY_NAME, "Local"),
                        x509.NameAttribute(
                            NameOID.ORGANIZATION_NAME, "Synthetic Heart"
                        ),
                        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
                    ]
                )
                cert = (
                    x509.CertificateBuilder()
                    .subject_name(subject)
                    .issuer_name(issuer)
                    .public_key(key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(datetime.datetime.utcnow())
                    .not_valid_after(
                        datetime.datetime.utcnow() + datetime.timedelta(days=3650)
                    )
                    .add_extension(
                        x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                        critical=False,
                    )
                    .sign(key, hashes.SHA256())
                )
                with open(default_key, "wb") as f:
                    f.write(
                        key.private_bytes(
                            encoding=serialization.Encoding.PEM,
                            format=serialization.PrivateFormat.TraditionalOpenSSL,
                            encryption_algorithm=serialization.NoEncryption(),
                        )
                    )
                with open(default_cert, "wb") as f:
                    f.write(cert.public_bytes(serialization.Encoding.PEM))
                self.tls_certfile = default_cert
                self.tls_keyfile = default_key
                log_info(
                    f"{LOG_PREFIX} Generated self-signed TLS certificate using cryptography at {default_cert}"
                )
                return
            except Exception as exc:
                raise RuntimeError(f"cryptography generation failed: {exc}")

        # If openssl exists, use it to generate cert/key. Support SANs via
        # SYNTH_WEBUI_CERT_SANS env var (comma-separated list of entries like
        # 'IP:192.168.1.42,DNS:localhost'). If no SANs provided, fall back to
        # sensible defaults (localhost + 127.0.0.1).
        subj = os.getenv("SYNTH_WEBUI_CERT_SUBJ", "/CN=localhost")
        days = os.getenv("SYNTH_WEBUI_CERT_DAYS", "3650")

        # Build SAN string
        raw_sans = os.getenv("SYNTH_WEBUI_CERT_SANS", None)
        if raw_sans and raw_sans.strip():
            sans_string = raw_sans.strip()
        else:
            # Default SANs (DNS:localhost, IP:127.0.0.1)
            sans_string = "DNS:localhost,IP:127.0.0.1"
            # If SYNTH_WEBUI_HOST looks like an IP, include it too
            synth_host = os.getenv("SYNTH_WEBUI_HOST")
            if synth_host:
                import ipaddress

                try:
                    ipaddress.ip_address(synth_host)
                    sans_string = sans_string + f",IP:{synth_host}"
                except Exception:
                    # not an IP, could be hostname - include as DNS
                    sans_string = sans_string + f",DNS:{synth_host}"

        cmd = [
            openssl_path,
            "req",
            "-x509",
            "-nodes",
            "-days",
            str(days),
            "-newkey",
            "rsa:2048",
            "-keyout",
            default_key,
            "-out",
            default_cert,
            "-subj",
            subj,
            "-addext",
            f"subjectAltName={sans_string}",
        ]
        try:
            log_info(
                f"{LOG_PREFIX} Generating self-signed certificate using openssl at {default_cert} (SAN={sans_string})"
            )
            subprocess.run(cmd, check=True)
            self.tls_certfile = default_cert
            self.tls_keyfile = default_key
            log_info(
                f"{LOG_PREFIX} Self-signed certificate generated: cert={self.tls_certfile} key={self.tls_keyfile}"
            )
        except Exception as exc:
            # If openssl failed with addext (older openssl), try fallback via
            # creating a tmp config file with v3 extensions
            try:
                import tempfile

                with tempfile.NamedTemporaryFile("w", delete=False) as cfg:
                    cfg.write(
                        "[req]\ndistinguished_name=req\n[ v3_ca ]\nsubjectAltName="
                        + sans_string
                        + "\n"
                    )
                    cfg_path = cfg.name
                cmd2 = [
                    openssl_path,
                    "req",
                    "-x509",
                    "-nodes",
                    "-days",
                    str(days),
                    "-newkey",
                    "rsa:2048",
                    "-keyout",
                    default_key,
                    "-out",
                    default_cert,
                    "-subj",
                    subj,
                    "-extensions",
                    "v3_ca",
                    "-config",
                    cfg_path,
                ]
                subprocess.run(cmd2, check=True)
                self.tls_certfile = default_cert
                self.tls_keyfile = default_key
                log_info(
                    f"{LOG_PREFIX} Self-signed certificate generated using fallback config: cert={self.tls_certfile} key={self.tls_keyfile}"
                )
            except Exception as exc2:
                raise RuntimeError(
                    f"Failed to generate self-signed certificate: {exc} / {exc2}"
                )

    async def start(self) -> None:
        """Start the web UI interface if autostart is enabled."""
        try:
            log_info(
                f"{LOG_PREFIX} start() called - initializing persona manager and starting server if enabled"
            )
            # Initialize persona manager now that core initialization is complete
            if not hasattr(self, "persona_manager") or self.persona_manager is None:
                from core.persona_manager import get_persona_manager

                try:
                    self.persona_manager = get_persona_manager()
                    if self.persona_manager:
                        try:
                            self.persona_manager.set_webui(self)
                            self.persona_manager.set_animation_handler(
                                self.animation_handler
                            )
                        except Exception as pm_exc:
                            log_warning(
                                f"{LOG_PREFIX} Persona manager set_* calls failed: {pm_exc}"
                            )
                        log_info(f"{LOG_PREFIX} Persona manager initialized")
                    else:
                        log_warning(
                            f"{LOG_PREFIX} Failed to initialize persona manager"
                        )
                except Exception as pm_exc_outer:
                    log_warning(
                        f"{LOG_PREFIX} Exception while getting persona manager: {pm_exc_outer}"
                    )
                    self.persona_manager = None

            # Start the periodic Karada touch-event cleanup task.
            try:
                from core.karada_touch_events import start_cleanup_task

                start_cleanup_task()
                log_debug(f"{LOG_PREFIX} Karada touch-event cleanup task started")
            except Exception as kte_exc:
                log_warning(
                    f"{LOG_PREFIX} Failed to start Karada cleanup task: {kte_exc}"
                )

            if self.autostart:
                log_info(
                    f"{LOG_PREFIX} Autostart enabled, starting {BRAND_NAME} server"
                )
                try:
                    self.start_server_async()
                    log_info(f"{LOG_PREFIX} start() completed - server start scheduled")
                except Exception as start_exc:
                    import traceback

                    log_error(
                        f"{LOG_PREFIX} Exception while invoking start_server_async: {start_exc}"
                    )
                    log_error(f"{LOG_PREFIX} Traceback: {traceback.format_exc()}")
            else:
                log_info(f"{LOG_PREFIX} Autostart disabled, skipping server start")
        except Exception as exc:
            import traceback

            log_error(f"{LOG_PREFIX} Exception in start(): {exc}")
            log_error(f"{LOG_PREFIX} Traceback: {traceback.format_exc()}")

    # ------------------------------------------------------------------
    # HTML template
    # ------------------------------------------------------------------
    def _render_logs(self) -> str:
        template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{brand_name} Logs</title>
    <style>
        body {{ background: #101017; color: #e0ffe0; font-family: monospace; margin: 0; }
        header {{ padding: 1rem 1.5rem; background: #1b1b28; display: flex; flex-wrap: wrap; justify-content: space-between; gap: 1rem; align-items: center; }
        header .left {{ display: flex; gap: 1rem; align-items: center; }
        header .filters {{ display: flex; gap: 0.75rem; align-items: center; flex-wrap: wrap; font-size: 0.9rem; }
        header label {{ display: inline-flex; align-items: center; gap: 0.35rem; cursor: pointer; background: rgba(255, 255, 255, 0.08); padding: 0.35rem 0.6rem; border-radius: 999px; }
        header input[type="checkbox"] {{ accent-color: #ff6bd6; }
        main {{ padding: 1.5rem; }
        pre {{
            background: #09090f;
            border-radius: 12px;
            padding: 1.2rem;
            height: 80vh;
            overflow-y: auto;
            white-space: pre-wrap;
        }
        a {{ color: #9fa8ff; text-decoration: none; }
        a:hover {{ text-decoration: underline; }
    </style>
</head>
<body>
    <header>
        <div class="left">
            <strong>Realtime logs</strong>
            <a href="/">Back to chat</a>
        </div>
        <div class="filters">
            <label><input class="level-filter" data-level="info" type="checkbox" checked />INFO</label>
            <label><input class="level-filter" data-level="warning" type="checkbox" checked />WARNING</label>
            <label><input class="level-filter" data-level="error" type="checkbox" checked />ERROR</label>
            <label><input class="level-filter" data-level="debug" type="checkbox" checked />DEBUG</label>
        </div>
    </header>
    <main>
        <pre id="log"></pre>
    </main>
    <script>
        const log = document.getElementById('log');
        const filters = document.querySelectorAll('.level-filter');
        const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
        const ws = new WebSocket(`${protocol}://${window.location.host}/logs`);
        const levels = {{ info: true, warning: true, error: true, debug: true }};
        const lines = [];

        const levelFromLine = (line) => {{
            const match = line.match(/\\[(INFO|WARNING|ERROR|DEBUG)\\]/i);
            return match ? match[1].toLowerCase() : 'info';
        }};

        const render = () => {{
            const filtered = lines.filter((line) => {{
                const lvl = levelFromLine(line);
                return levels[lvl] ?? true;
            }});
            log.textContent = filtered.join('\\n');
            log.scrollTop = log.scrollHeight;
        }};

        filters.forEach((checkbox) => {{
            checkbox.addEventListener('change', (event) => {{
                const level = event.target.dataset.level;
                levels[level] = event.target.checked;
                render();
            }});
        }});

        ws.addEventListener('message', (event) => {{
            lines.push(event.data);
            render();
        }});
    </script>
</body>
</html>
"""
        return template.replace("{brand_name}", BRAND_NAME)

    def _render_diary(self) -> str:
        template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{brand_name} Diary</title>
    <style>
        body {{ background: #101017; color: #e0ffe0; font-family: monospace; margin: 0; padding: 1rem; }}
        .diary-container {{ 
            background: #1b1b28; 
            border-radius: 12px; 
            padding: 1.5rem; 
            max-width: 1200px; 
            margin: 0 auto; 
        }}
        .diary-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }}
        .diary-controls {{ display: flex; gap: 1rem; align-items: center; }}
        .diary-controls input, .diary-controls select {{ padding: 0.5rem; border-radius: 6px; border: 1px solid #444; background: #2a2a3a; color: #e0ffe0; }}
        .diary-controls button {{ padding: 0.5rem 1rem; border-radius: 6px; border: 1px solid #444; background: #2a2a3a; color: #e0ffe0; cursor: pointer; }}
        .diary-controls button:hover {{ background: #3a3a4a; }}
        .diary-entries {{ max-height: 70vh; overflow-y: auto; }}
        .diary-date-group {{ margin-bottom: 1rem; border: 1px solid #444; border-radius: 8px; }}
        .diary-date-header {{ background: #2a2a3a; padding: 0.75rem; cursor: pointer; display: flex; justify-content: space-between; }}
        .diary-date-content {{ padding: 0; }}
        .diary-entry {{ padding: 1rem; border-bottom: 1px solid #333; display: flex; gap: 1rem; }}
        .diary-entry:last-child {{ border-bottom: none; }}
        .diary-entry-checkbox {{ display: none; }}
        .diary-entry-content {{ flex: 1; }}
        .diary-entry-meta {{ font-size: 0.85rem; color: #aaa; margin-bottom: 0.5rem; }}
        .diary-entry-text {{ line-height: 1.5; white-space: pre-wrap; }}
        .loading {{ text-align: center; padding: 2rem; color: #aaa; }}
        .error {{ color: #ff6b6b; padding: 1rem; background: rgba(255, 107, 107, 0.1); border-radius: 6px; }}
    </style>
</head>
<body>
    <div class="diary-container">
        <div class="diary-header">
            <h2>AI Diary</h2>
            <div class="diary-controls">
                <input type="text" id="diary-search" placeholder="Search diary entries..." />
                <label><input type="checkbox" id="group-by-date" checked /> Group by date</label>
                <button id="edit-mode-btn">Edit</button>
                <button id="archive-btn" style="display: none;">Archive Selected</button>
                <button id="unarchive-btn" style="display: none;">Unarchive Selected</button>
                <button id="delete-btn" style="display: none; background: #ff4757;">Delete Selected</button>
            </div>
        </div>
        <div id="diary-entries" class="diary-entries">
            <div class="loading">Loading diary entries...</div>
        </div>
    </div>
    <script>
        let diaryEntries = [];
        let editMode = false;
        let selectedEntries = new Set();

        async function loadDiaryEntries() {{
            try {{
                const response = await fetch('/api/diary?days=365&limit=1000');
                const data = await response.json();
                
                if (data.diary && data.diary.entries) {{
                    diaryEntries = data.diary.entries;
                    renderDiaryEntries();
                }} else {{
                    document.getElementById('diary-entries').innerHTML = '<div class="error">Failed to load diary entries</div>';
                }}
            }} catch (error) {{
                console.error('Error loading diary entries:', error);
                document.getElementById('diary-entries').innerHTML = '<div class="error">Error loading diary entries</div>';
            }}
        }}

        function renderDiaryEntries() {{
            const container = document.getElementById('diary-entries');
            const searchTerm = document.getElementById('diary-search').value.toLowerCase();
            const groupByDate = document.getElementById('group-by-date').checked;
            
            let filteredEntries = diaryEntries.filter(entry => {{
                const text = (entry.content + ' ' + (entry.personal_thought || '') + ' ' + (entry.interaction_summary || '')).toLowerCase();
                return text.includes(searchTerm);
            }});
            
            if (!groupByDate) {{
                const html = filteredEntries.map(entry => renderDiaryEntry(entry)).join('');
                container.innerHTML = html || '<div class="loading">No entries found</div>';
                return;
            }}
            
            // Group by date
            const groups = {{}};
            filteredEntries.forEach(entry => {{
                const date = new Date(entry.timestamp).toDateString();
                if (!groups[date]) groups[date] = [];
                groups[date].push(entry);
            }});
            
            const html = Object.keys(groups).sort((a, b) => new Date(b) - new Date(a)).map(date => {{
                const entries = groups[date];
                return `
                    <div class="diary-date-group">
                        <div class="diary-date-header" onclick="toggleDateGroup(this)">
                            <span>${{date}}</span>
                        </div>
                        <div class="diary-date-content">
                            ${entries.map(e => renderDiaryEntry(e)).join('')}
                        </div>
                    </div>
                `;
            }).join('');
            container.innerHTML = html || '<div class="loading">No entries found</div>';
        }
    </script>
</body>
</html>
"""
        return template.replace("{brand_name}", BRAND_NAME)

    async def list_skins(self):
        """List available skins (folders under skins).

        Returns: JSON list with entries: name, version, author, description, preview_url, vrm_present
        Reads metadata from persona.json if available.
        """
        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        result = []
        if not skins_dir.exists():
            return JSONResponse(result)

        for entry in sorted(skins_dir.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name == "temp":
                continue

            preview = None
            vrm_present = False
            version = None
            author = None
            description = None
            vrm_version = None

            try:
                # Check for preview image
                preview_path = entry / "preview.png"
                if preview_path.exists():
                    preview = f"/skins/{name}/preview.png"

                # check for vrm - only check in direct directory, not recursive, for speed
                vrm_file_path = None
                for v in entry.glob("*.vrm"):
                    vrm_present = True
                    vrm_file_path = v
                    break

                if vrm_file_path:
                    vrm_version = self._detect_vrm_version(vrm_file_path)

                # Try to load metadata from persona.json
                persona_json_path = entry / "persona.json"
                if persona_json_path.exists():
                    try:
                        import json

                        persona_data = json.loads(
                            persona_json_path.read_text(encoding="utf-8")
                        )
                        # Extract metadata from persona.json
                        version = persona_data.get("version")
                        author = persona_data.get("author")
                        description = persona_data.get("description")
                        # Override vrm_version if explicitly set in JSON
                        if "vrm_version" in persona_data:
                            vrm_version = persona_data["vrm_version"]
                        # Use name from JSON if available
                        if not name or name == entry.name:
                            name = persona_data.get("name", entry.name)
                    except Exception as e:
                        log_debug(
                            f"[webui] Error reading persona.json for skin '{entry.name}': {e}"
                        )
            except Exception as e:
                log_warning(f"[webui] Error scanning skin '{name}': {e}")

            result.append(
                {
                    "name": name,
                    "folder": entry.name,  # Keep original folder name for reference
                    "version": version,
                    "vrm_version": vrm_version,
                    "author": author,
                    "description": description,
                    "preview_url": preview,
                    "vrm_present": vrm_present,
                    "valid": vrm_present,
                }
            )

        # Ensure Rei exists and is valid
        rei = next((s for s in result if s["folder"] == "Rei"), None)
        if not rei:
            raise HTTPException(status_code=500, detail="Default skin 'Rei' missing")
        if not rei.get("valid"):
            raise HTTPException(
                status_code=500, detail="Default skin 'Rei' invalid (missing VRM)"
            )

        return JSONResponse(result)

    async def get_suggested_locations(self):
        """Return a list of suggested locations derived from timezone database.

        Locations are formatted as "City,Country" pairs extracted from timezone names.
        """
        try:
            from core.time_zone_utils import get_suggested_locations

            locations = get_suggested_locations()
            return JSONResponse({"locations": locations, "count": len(locations)})
        except Exception as e:
            log_error(f"{LOG_PREFIX} Error getting suggested locations: {e}")
            return JSONResponse(
                {"locations": [], "count": 0, "error": str(e)}, status_code=500
            )

    async def get_current_weather_endpoint(self):
        """GET /api/weather/current — return the current weather from WeatherPlugin."""
        from core.core_initializer import PLUGIN_REGISTRY

        weather_plugin = None
        try:
            if isinstance(PLUGIN_REGISTRY, dict):
                weather_plugin = PLUGIN_REGISTRY.get("weather")
        except Exception as e:
            log_error(f"{LOG_PREFIX} Error accessing PLUGIN_REGISTRY: {e}")

        if not weather_plugin:
            return JSONResponse(
                {
                    "status": "error",
                    "message": "Weather plugin not available",
                },
                status_code=503,
            )

        if not hasattr(weather_plugin, "get_current_weather"):
            return JSONResponse(
                {
                    "status": "error",
                    "message": "Weather plugin does not support current weather endpoint",
                },
                status_code=501,
            )

        try:
            result = await weather_plugin.get_current_weather()
            return JSONResponse(result)
        except Exception as e:
            log_error(f"{LOG_PREFIX} Error in get_current_weather_endpoint: {e}")
            return JSONResponse(
                {
                    "status": "error",
                    "message": "Failed to fetch current weather",
                    "error": str(e),
                },
                status_code=500,
            )

    async def clear_uploaded_vrm(self):
        """Clear any user-uploaded VRM in skins/temp/model.vrm and restore Rei's VRM.

        This sets the active VRM to the restored model.
        """
        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        rei_dir = skins_dir / "Rei"
        if not rei_dir.exists() or not rei_dir.is_dir():
            raise HTTPException(status_code=500, detail="Default skin 'Rei' missing")

        # find VRM inside Rei
        rei_vrm = None
        for p in rei_dir.rglob("*.vrm"):
            rei_vrm = p
            break
        if not rei_vrm:
            raise HTTPException(
                status_code=500, detail="Default skin 'Rei' has no VRM to restore"
            )

        temp_dir = (
            Path(__file__).resolve().parent.parent
            / "res"
            / "synth_webui"
            / "skins"
            / "temp"
        )
        temp_dir.mkdir(parents=True, exist_ok=True)
        target = temp_dir / "model.vrm"
        import shutil

        try:
            shutil.copy2(rei_vrm, target)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to restore Rei VRM to temp: {exc}")
            raise HTTPException(status_code=500, detail="Failed to restore default VRM")

        try:
            self._set_active_vrm("model.vrm")
            # after restoring Rei we want the skin hint to reflect that
            self._current_skin = "Rei"
        except Exception:
            pass

        return JSONResponse(
            {"status": "ok", "restored_from": str(rei_vrm)}, status_code=200
        )

    async def activate_skin(self, skin_name: str):
        """Activate a skin by copying its VRM into avatars and setting it active.

        Returns 201 with name if activated, 404 if no VRM found.
        """
        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        target_skin = skins_dir / Path(skin_name).name
        if not target_skin.exists() or not target_skin.is_dir():
            raise HTTPException(status_code=404, detail="Skin not found")

        # find a .vrm file inside skin
        vrm_file = None
        for p in target_skin.rglob("*.vrm"):
            vrm_file = p
            break
        if not vrm_file:
            raise HTTPException(status_code=404, detail="No VRM found in skin")

        # copy to avatars and set active
        avatars_dir = self.vrm_dir
        avatars_dir.mkdir(parents=True, exist_ok=True)
        safe_name = self._sanitize_vrm_filename(vrm_file.name)
        target = avatars_dir / safe_name
        import shutil

        try:
            shutil.copy2(vrm_file, target)
        except Exception as exc:
            log_error(
                f"{LOG_PREFIX} Failed to copy VRM when activating skin {skin_name}: {exc}"
            )
            raise HTTPException(status_code=500, detail="Failed to activate skin")

        try:
            self._set_active_vrm(target.name)
            # remember which skin we just activated; the active_vrm itself is
            # just a filename once copied into avatars/, so we wouldn't be able
            # to infer it later.
            self._current_skin = skin_name
        except Exception as exc:
            log_error(
                f"{LOG_PREFIX} Failed to set active VRM after activating skin {skin_name}: {exc}"
            )
            raise HTTPException(status_code=500, detail="Failed to activate skin")

        # Broadcast the new model so connected clients reload it live. Without
        # this only clients that poll (legacy webui's refreshModels) notice the
        # swap; the stage frontend relies entirely on the vrm_model broadcast.
        try:
            if self.animation_handler:
                await self.animation_handler.set_vrm_model(
                    f"/avatars/{target.name}", target.name
                )
                log_debug(f"{LOG_PREFIX} Broadcast vrm_model: {target.name}")
        except Exception as vrm_exc:
            log_warning(f"{LOG_PREFIX} Failed to broadcast vrm_model: {vrm_exc}")

        # Trigger skin_change animation so the avatar plays a transition animation
        # after the frontend reloads the VRM model.
        try:
            handler = getattr(self, "animation_handler", None)
            if handler and hasattr(handler, "play_animation"):
                asyncio.ensure_future(
                    handler.play_animation(
                        AnimationState.SKIN_CHANGE,
                        session_id=None,
                        loop=False,
                        source="skin_activation",
                    )
                )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Failed to trigger skin_change animation: {exc}")

        return JSONResponse({"status": "ok", "name": target.name}, status_code=201)

    # ------------------------------------------------------------------
    # Skin editor endpoints
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_skin_folder_name(name: str) -> str:
        """Sanitize a skin name into a safe folder name."""
        safe = "".join(
            ch for ch in name if ch.isalnum() or ch in ("-", "_", " ")
        ).strip()
        # Replace spaces with underscores
        safe = safe.replace(" ", "_")
        if not safe:
            safe = f"skin_{uuid.uuid4().hex[:8]}"
        return safe

    async def create_skin(self, request: Request) -> JSONResponse:
        """Create a new skin folder with a persona.json.

        Expects JSON body: {name, author?, version?, appearance?}
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Skin name is required")

        author = (body.get("author") or "").strip()
        version = (body.get("version") or "1.0").strip()
        appearance = (body.get("appearance") or "").strip()

        folder_name = self._sanitize_skin_folder_name(name)
        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        skin_path = skins_dir / folder_name

        if skin_path.exists():
            raise HTTPException(
                status_code=409,
                detail=f"A skin folder '{folder_name}' already exists",
            )

        try:
            skin_path.mkdir(parents=True, exist_ok=False)
            persona = {
                "name": name,
                "description": f"Custom skin: {name}",
                "version": version,
                "author": author,
                "attributes": {"appearance": appearance} if appearance else {},
            }
            persona_path = skin_path / "persona.json"
            persona_path.write_text(json.dumps(persona, indent=4), encoding="utf-8")
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to create skin '{name}': {exc}")
            raise HTTPException(status_code=500, detail="Failed to create skin")

        log_info(f"{LOG_PREFIX} Created new skin '{name}' at {skin_path}")
        return JSONResponse(
            {"status": "ok", "folder": folder_name, "name": name}, status_code=201
        )

    async def upload_skin_vrm(
        self, skin_name: str, file: UploadFile = File(...)
    ) -> JSONResponse:
        """Upload a VRM file into an existing skin folder."""
        if not file.filename or not file.filename.lower().endswith(".vrm"):
            raise HTTPException(status_code=400, detail="Only .vrm files are accepted")

        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        skin_path = skins_dir / Path(skin_name).name
        if not skin_path.exists() or not skin_path.is_dir():
            raise HTTPException(status_code=404, detail="Skin not found")

        # Remove any existing VRM files in the skin folder
        for existing_vrm in skin_path.glob("*.vrm"):
            try:
                existing_vrm.unlink()
            except Exception:
                pass

        target = skin_path / "model.vrm"
        try:
            with target.open("wb") as f:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        finally:
            await file.close()

        log_info(f"{LOG_PREFIX} Uploaded VRM to skin '{skin_name}'")
        return JSONResponse({"status": "ok", "skin": skin_name}, status_code=201)

    async def upload_skin_preview(
        self, skin_name: str, file: UploadFile = File(...)
    ) -> JSONResponse:
        """Upload a preview image into an existing skin folder."""
        if not file.filename:
            raise HTTPException(status_code=400, detail="No file provided")

        lower = file.filename.lower()
        if not any(lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp")):
            raise HTTPException(
                status_code=400,
                detail="Only image files (.png, .jpg, .jpeg, .webp) are accepted",
            )

        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        skin_path = skins_dir / Path(skin_name).name
        if not skin_path.exists() or not skin_path.is_dir():
            raise HTTPException(status_code=404, detail="Skin not found")

        target = skin_path / "preview.png"
        try:
            with target.open("wb") as f:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        finally:
            await file.close()

        log_info(f"{LOG_PREFIX} Uploaded preview to skin '{skin_name}'")
        return JSONResponse({"status": "ok", "skin": skin_name}, status_code=201)

    async def download_skin(self, skin_name: str) -> FileResponse:
        """Download a skin folder as a .zip archive."""
        import shutil
        import tempfile

        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        skin_path = skins_dir / Path(skin_name).name
        if not skin_path.exists() or not skin_path.is_dir():
            raise HTTPException(status_code=404, detail="Skin not found")

        try:
            tmp_dir = tempfile.mkdtemp()
            archive_base = Path(tmp_dir) / skin_path.name
            archive_path = shutil.make_archive(
                str(archive_base),
                "zip",
                root_dir=str(skins_dir),
                base_dir=skin_path.name,
            )
            return FileResponse(
                path=archive_path,
                filename=f"{skin_path.name}.zip",
                media_type="application/zip",
            )
        except Exception as exc:
            log_error(
                f"{LOG_PREFIX} Failed to create skin archive for '{skin_name}': {exc}"
            )
            raise HTTPException(status_code=500, detail="Failed to create skin archive")

    async def upload_skin_zip(self, file: UploadFile = File(...)) -> JSONResponse:
        """Upload a .zip skin pack and extract it into skins/."""
        import shutil
        import tempfile
        import zipfile

        if not file.filename or not file.filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="Only .zip files are accepted")

        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        skins_dir.mkdir(parents=True, exist_ok=True)

        # Save to temp file
        tmp_dir = tempfile.mkdtemp()
        temp_path = Path(tmp_dir) / "upload.zip"
        try:
            with temp_path.open("wb") as f:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        finally:
            await file.close()

        try:
            with zipfile.ZipFile(temp_path, "r") as zf:
                # Determine root folder from archive
                names = zf.namelist()
                if not names:
                    raise HTTPException(status_code=400, detail="Empty zip file")

                # Find root folder(s)
                root_candidates = {n.split("/")[0] for n in names if "/" in n}
                if len(root_candidates) == 1:
                    root = root_candidates.pop()
                else:
                    root = Path(file.filename).stem

                dest = skins_dir / root
                if dest.exists():
                    # Overwrite existing skin (except Rei)
                    if root == "Rei":
                        raise HTTPException(
                            status_code=403,
                            detail="Cannot overwrite the default Rei skin",
                        )
                    shutil.rmtree(dest)

                zf.extractall(skins_dir)

                # Validate: must have at least a persona.json or .vrm
                has_persona = (dest / "persona.json").exists()
                has_vrm = any(dest.glob("*.vrm"))
                if not has_persona and not has_vrm:
                    # Check one level deeper (zip might have nested structure)
                    nested = list(dest.iterdir())
                    if len(nested) == 1 and nested[0].is_dir():
                        inner = nested[0]
                        has_persona = (inner / "persona.json").exists()
                        has_vrm = any(inner.glob("*.vrm"))
                        if has_persona or has_vrm:
                            # Move inner contents up
                            for item in inner.iterdir():
                                shutil.move(str(item), str(dest / item.name))
                            inner.rmdir()

                if not (dest / "persona.json").exists() and not any(dest.glob("*.vrm")):
                    shutil.rmtree(dest, ignore_errors=True)
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid skin pack: must contain persona.json or a .vrm file",
                    )

        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid zip file")
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to extract skin zip: {exc}")
            raise HTTPException(status_code=500, detail="Failed to extract skin pack")
        finally:
            # Clean up temp file
            try:
                temp_path.unlink(missing_ok=True)
                Path(tmp_dir).rmdir()
            except Exception:
                pass

        log_info(f"{LOG_PREFIX} Uploaded skin pack '{root}'")
        return JSONResponse({"status": "ok", "folder": root}, status_code=201)

    async def delete_skin(self, skin_name: str) -> JSONResponse:
        """Delete a skin folder. Rei is protected and cannot be deleted."""
        import shutil

        safe_name = Path(skin_name).name
        if safe_name == "Rei":
            raise HTTPException(
                status_code=403,
                detail="The default skin 'Rei' cannot be deleted",
            )

        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        skin_path = skins_dir / safe_name
        if not skin_path.exists() or not skin_path.is_dir():
            raise HTTPException(status_code=404, detail="Skin not found")

        try:
            shutil.rmtree(skin_path)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to delete skin '{safe_name}': {exc}")
            raise HTTPException(status_code=500, detail="Failed to delete skin")

        log_info(f"{LOG_PREFIX} Deleted skin '{safe_name}'")
        return JSONResponse({"status": "ok", "deleted": safe_name})

    # ------------------------------------------------------------------
    # WebSocket logic
    # ------------------------------------------------------------------


async def start_server() -> None:
    """Compatibility helper to run the Synthetic Heart Web UI server in the foreground."""
    if synth_webui_interface is None:
        raise RuntimeError("WebUI interface is not initialized")

    if not synth_webui_interface.autostart:
        await synth_webui_interface._run_server()
        return

    # If autostart is enabled we already spawned the background server. Keep
    # the coroutine alive so ``uvicorn`` keeps running until interrupted.
    event = asyncio.Event()
    await event.wait()


# Global interface instance - created during initialize_interface()
synth_webui_interface = None


def initialize_interface():
    """Initialize the WebUI interface after config has been loaded from DB.

    This function is called by the core initializer after all configurations
    have been loaded from the database. This ensures that config_registry.get_var()
    returns the correct values from the DB.

    Can also be called to reload the interface when configuration changes.
    """
    global synth_webui_interface

    # If interface already exists, clean it up first
    if synth_webui_interface is not None:
        log_info(f"{LOG_PREFIX} Reloading interface with updated configuration...")
        shutdown_interface()

    log_info(f"{LOG_PREFIX} Creating {BRAND_NAME} interface instance...")
    synth_webui_interface = SynthWebUIInterface()
    # In some environments the automatic registration in __init__ may not run
    # (e.g., hot reload or partial initialization). Ensure explicit registration
    # here so the core initializer can start the interface and the server will
    # be started on the expected port (e.g. SYNTH_WEBUI_HTTPS_PORT -> 9009).
    try:
        from core.core_initializer import register_interface

        register_interface(INTERFACE_NAME, synth_webui_interface)
        log_info(
            f"{LOG_PREFIX} Interface registered (initialize_interface)",
            log_file=WEBUI_LOG,
        )
    except Exception as exc:
        log_warning(
            f"{LOG_PREFIX} Interface registration from initialize_interface failed: {exc}",
            log_file=WEBUI_LOG,
        )

    log_info(f"{LOG_PREFIX} {BRAND_NAME} interface instance created")

    return synth_webui_interface


def shutdown_interface():
    """Shutdown and cleanup the WebUI interface.

    Called before reload or shutdown to properly cleanup resources.
    """
    global synth_webui_interface

    if synth_webui_interface is None:
        log_debug(f"{LOG_PREFIX} No interface to shutdown")
        return

    log_info(f"{LOG_PREFIX} Shutting down {BRAND_NAME} interface...")

    try:
        # Stop the server if it's running
        synth_webui_interface.cleanup()
        log_info(f"{LOG_PREFIX} {BRAND_NAME} interface shutdown completed")
    except Exception as e:
        log_error(f"{LOG_PREFIX} Error during interface shutdown: {e}")

    synth_webui_interface = None


# Backward-compatible alias used by older code/tests.
WebUI = SynthWebUIInterface
