"""FastAPI-based web interface branded as the SyntH Web UI.

This is a core component of SyntH that provides a functional chat front-end
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
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, Optional, List, Any

from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    UploadFile,
    File,
    Request,
    HTTPException,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.core_initializer import register_interface
from core.logging_utils import _LOG_FILE, log_debug, log_error, log_info, log_warning
from core.config_manager import config_registry
from core.message_chain import get_failed_message_text, RESPONSE_TIMEOUT, FAILED_MESSAGE_TEXT
import core.plugin_instance as plugin_instance
from core import db as core_db
from core.animation_handler import get_animation_handler, AnimationState
from core.persona_manager import get_persona_manager
from core.action_state_manager import get_action_state_manager, AnimationPhase
import mimetypes


BRAND_NAME = "SyntH Web UI"
INTERFACE_NAME = "synth_webui"
LOG_PREFIX = "[synth_webui]"
_LEGACY_AUTOSTART_ENV = "WEBWAIFU_AUTOSTART"
_AUTOSTART_ENV = "SYNTH_WEBUI_AUTOSTART"
_LEGACY_VRM_DIR_ENV = "WEBWAIFU_VRM_DIR"
_VRM_DIR_ENV = "SYNTH_WEBUI_VRM_DIR"


# Ensure correct MIME types are registered
mimetypes.init()
mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/javascript', '.mjs')
mimetypes.add_type('application/json', '.json')


class SynthWebUIInterface:
    """Production-ready web interface served from the Docker container."""
    
    display_name = "Web UI"

    def __init__(self) -> None:
        self.app = FastAPI(title=BRAND_NAME, version="1.0")
        self.start_time = datetime.utcnow()
        self.connections: Dict[str, WebSocket] = {}
        self.message_history: Dict[str, Deque[dict]] = {}
        self.max_history = 100
        # Runtime/configurable attributes with sensible defaults
        # The Web UI must always autostart; do not allow runtime toggle.
        self.autostart = True
        self.host = os.getenv('SYNTH_WEBUI_HOST', '0.0.0.0')
        try:
            self.port = int(os.getenv('SYNTH_WEBUI_PORT', os.getenv('PORT', '8080')))
        except Exception:
            self.port = 8080
        self.log_level = os.getenv('SYNTH_WEBUI_LOG_LEVEL', 'info')
        # Selkies desktop ports used for UI hints
        try:
            self.selkies_https_port = int(os.getenv('SELKIES_HTTPS_PORT', '3000'))
        except Exception:
            self.selkies_https_port = 3000
        try:
            self.selkies_http_port = int(os.getenv('SELKIES_HTTP_PORT', '3001'))
        except Exception:
            self.selkies_http_port = 3001
        # Log streaming options
        self.log_source_path = None
        self.log_wait_seconds = 20
        # Server control placeholders
        self._server_lock = threading.Lock()
        self._server = None
        self._server_thread = None
        self._server_task = None

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
            self.vrm_dir = Path('skins/temp')
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

        if static_dir.exists():
            self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        else:
            log_warning(f"{LOG_PREFIX} static directory not found: {static_dir}")
        
        # Mount JS directory for Mixamo animations (separate mount to avoid path conflicts)
        js_dir = Path(__file__).resolve().parent.parent / "res" / "synth_webui" / "js"
        if js_dir.exists():
            self.app.mount("/js", StaticFiles(directory=str(js_dir)), name="synth-webui-js")
            log_info(f"{LOG_PREFIX} Mounted /js to {js_dir}")
        else:
            log_warning(f"{LOG_PREFIX} JS directory not found: {js_dir}")
        
        # No global animations directory: animations live inside each skin under /skins/<skin>/animations

        # Mount skins directory (contains per-skin assets: preview, animations, md)
        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        if skins_dir.exists():
            try:
                self.app.mount("/skins", StaticFiles(directory=str(skins_dir)), name="synth-webui-skins")
                log_info(f"{LOG_PREFIX} Mounted /skins to {skins_dir}")
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Failed to mount /skins: {exc}")
        else:
            log_warning(f"{LOG_PREFIX} Skins directory not found: {skins_dir}")
        if self.vrm_dir.exists():
            log_debug(f"{LOG_PREFIX} VRM directory is_dir: {self.vrm_dir.is_dir()}")
            log_debug(f"{LOG_PREFIX} VRM directory is readable: {os.access(self.vrm_dir, os.R_OK)}")
            
            try:
                files = list(self.vrm_dir.iterdir())
                log_info(f"{LOG_PREFIX} VRM directory contains {len(files)} items:")
                for item in files:
                    file_type = 'file' if item.is_file() else 'dir'
                    size = item.stat().st_size if item.is_file() else 'N/A'
                    log_info(f"{LOG_PREFIX}   - {item.name} ({file_type}, {size} bytes)")
            except Exception as list_exc:
                log_warning(f"{LOG_PREFIX} Unable to list VRM directory contents: {list_exc}")
        else:
            log_warning(f"{LOG_PREFIX} VRM directory does not exist, /avatars endpoint NOT mounted")
        
        log_info(f"{LOG_PREFIX} ========== VRM DIRECTORY MOUNT END ==========")


        self.app.get("/")(self.index)
        self.app.get("/health")(self.health)
        self.app.get("/api/action-state")(self.get_action_state_endpoint)
        self.app.get("/stats")(self.stats)
        self.app.get("/logs")(self.logs_page)
        self.app.get("/diary")(self.diary_page)
        self.app.websocket("/ws")(self.websocket_endpoint)
        self.app.websocket("/logs")(self.logs_ws_endpoint)
        self.app.get("/api/vrm")(self.list_vrm_models)
        self.app.get("/api/vrm/active")(self.get_active_vrm_endpoint)
        self.app.post("/api/vrm")(self.upload_vrm_model)
        self.app.post("/api/vrm/active")(self.set_active_vrm_endpoint)
        self.app.delete("/api/vrm/{model_name}")(self.delete_vrm_model)

        self.app.post("/api/persona")(self.upload_persona_pack)
        # Skins management endpoints
        self.app.get("/api/skins")(self.list_skins)
        self.app.post("/api/skins/{skin_name}/activate")(self.activate_skin)
        self.app.post("/api/skins/uploaded/clear")(self.clear_uploaded_vrm)
        self.app.get("/api/components")(self.components_summary)
        self.app.post("/api/components/reload")(self.reload_component)
        self.app.post("/api/components/dev/toggle")(self.toggle_dev_components)
        self.app.post("/api/system/restart")(self.restart_system)
        self.app.get("/api/config")(self.config_summary)
        # Debug endpoints (only enabled when WEB_DEBUG=1)
        self.app.get("/api/debug/db_pool")(self.db_pool_debug)
        self.app.post("/api/config")(self.update_config_entry)
        self.app.post("/api/components/llm")(self.set_llm_engine)
        self.app.get("/api/logchat/info")(self.get_logchat_info)
        self.app.get("/api/diary")(self.diary_summary)
        self.app.post("/api/diary/archive")(self.archive_diary_entries)
        self.app.post("/api/diary/unarchive")(self.unarchive_diary_entries)
        self.app.delete("/api/diary/archive")(self.delete_archived_entries)
        self.app.get("/api/selkies")(self.get_selkies_config)
        self.app.get("/api/animations/{skin}/{animation_type}")(self.get_animations_for_type)

        # Template sections route for modular loading
        self.app.get("/templates/{section}.html")(self.serve_template_section)

        register_interface(INTERFACE_NAME, self)
        log_info(f"{LOG_PREFIX} Interface registered")
        
        # Initialize animation handler
        self.animation_handler = get_animation_handler()
        self.animation_handler.set_webui(self)
        log_info(f"{LOG_PREFIX} Animation handler initialized")
        
        # Initialize global action state manager
        self.action_state_manager = get_action_state_manager()
        # Register callback to broadcast state changes to all WebSocket clients
        self.action_state_manager.register_state_changed_callback(self._broadcast_action_state)
        log_info(f"{LOG_PREFIX} Action state manager initialized with WebSocket broadcast")
        
        # Persona manager will be initialized in start() method after core initialization
        self.persona_manager = None
        
        if self.autostart:
            log_info(f"{LOG_PREFIX} Autostart enabled - will start server when event loop is available")
            # Don't start server here - it will be started by the main application
        else:
            log_info(f"{LOG_PREFIX} Autostart disabled - {BRAND_NAME} will not start automatically")

    # ------------------------------------------------------------------
    # Interface metadata
    # ------------------------------------------------------------------
    @staticmethod
    def get_interface_id() -> str:
        return INTERFACE_NAME

    @staticmethod
    def get_supported_actions() -> dict:
        return {
            "message_synth_webui": {
                "required_fields": ["text", "target"],
                "optional_fields": [],
                "description": f"Send a text message to a {BRAND_NAME} session.",
            }
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> dict:
        if action_name == "message_synth_webui":
            return {
                "description": f"Send a message to the {BRAND_NAME} browser client.",
                "payload": {
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
            # Read the template file - use synth_webui_index.html for the complete UI
            template_path = Path(__file__).parent / "webui_templates" / "synth_webui_index.html"
            with open(template_path, 'r', encoding='utf-8') as f:
                template = f.read()
            
            # Get replacement values
            from core.message_chain import RESPONSE_TIMEOUT, get_failed_message_text
            
            replacements = {
                '%%BRAND_NAME%%': BRAND_NAME,
                '%%LOGO_URL%%': '/static/synth_logo.png',  # Default logo path
                '%%RESPONSE_TIMEOUT%%': str(int(RESPONSE_TIMEOUT)),
                '%%FAILED_MESSAGE_TEXT%%': str(get_failed_message_text()),
                # Expose WEB_DEBUG flag to the template (default false)
                '%%WEB_DEBUG%%': '1' if os.getenv('WEB_DEBUG', '0') in ('1', 'true', 'True') else '0',
            }
            
            # Apply replacements
            for placeholder, value in replacements.items():
                template = template.replace(placeholder, value)
            
            return template
            
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to render index template: {exc}")
            # Fallback to a simple error page
            return f"""
<!DOCTYPE html>
<html>
<head><title>Error</title></head>
<body>
<h1>SyntH Web UI Error</h1>
<p>Failed to load the web interface: {exc}</p>
</body>
</html>
"""

    async def index(self):
        log_info(f"{LOG_PREFIX} Index route called")
        try:
            html = self._render_index()
            log_info(f"{LOG_PREFIX} Rendered HTML length: {len(html)}")
        except Exception as exc:
            log_error(f"{LOG_PREFIX} failed to render index: {exc}")
            raise HTTPException(status_code=500, detail="Unable to render SyntH Web UI") from exc
        return HTMLResponse(content=html)

    async def health(self):
        return JSONResponse({"status": "ok", "time": datetime.utcnow().isoformat()})

    async def get_action_state_endpoint(self):
        """Get the current global action state."""
        state = await self.action_state_manager.get_current_action()
        if state:
            return JSONResponse(state)
        else:
            return JSONResponse({
                "action_id": None,
                "phase": "IDLE",
                "component": None,
                "started_at": None
            })

    async def stats(self):
        uptime = int((datetime.utcnow() - self.start_time).total_seconds())
        return JSONResponse({"uptime": uptime, "sessions": len(self.connections)})

    async def db_pool_debug(self, request: Request):
        """Return debug information about the DB connection pool.

        This endpoint is intentionally gated by the WEB_DEBUG environment
        variable to avoid exposing internals in production by accident.
        """
        web_debug = os.getenv('WEB_DEBUG', '0').lower()
        if web_debug not in ('1', 'true', 'yes'):
            raise HTTPException(status_code=403, detail="Debug endpoints disabled")

        try:
            info = core_db.get_pool_debug_info()
            return JSONResponse(info)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to get DB pool debug info: {exc}")
            raise HTTPException(status_code=500, detail="Unable to retrieve DB debug info")

    async def logs_page(self):
        html = self._render_logs()
        return HTMLResponse(content=html)

    async def diary_page(self):
        html = self._render_diary()
        return HTMLResponse(content=html)

    async def serve_template_section(self, section: str):
        """Serve modular template sections for dynamic loading."""
        try:
            # Validate section name to prevent path traversal
            allowed_sections = {'home', 'logs', 'diary', 'config', 'components', 'about', 'navbar'}
            if section not in allowed_sections:
                raise HTTPException(status_code=404, detail="Template section not found")

            # Load template section
            template_path = Path(__file__).parent / "webui_templates" / "sections" / f"{section}.html"
            if not template_path.exists():
                raise HTTPException(status_code=404, detail="Template section not found")

            with open(template_path, 'r', encoding='utf-8') as f:
                template = f.read()

            # Apply basic replacements
            replacements = {
                '%%BRAND_NAME%%': BRAND_NAME,
            }

            for key, value in replacements.items():
                template = template.replace(key, str(value))

            return HTMLResponse(content=template)

        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Template section not found")
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to serve template section {section}: {exc}")
            raise HTTPException(status_code=500, detail="Unable to load template section")

    # ------------------------------------------------------------------
    # WebSocket logic
    # ------------------------------------------------------------------
    async def websocket_endpoint(self, websocket: WebSocket):
        await websocket.accept()
        session_id = str(uuid.uuid4())
        self.connections[session_id] = websocket
        self.message_history.setdefault(session_id, deque(maxlen=self.max_history))
        await websocket.send_json({"type": "session", "session_id": session_id})
        await self._replay_history(session_id)
        
        # Set initial idle animation for new session
        try:
            if self.persona_manager:
                await self.persona_manager.set_animation_state("idle", session_id=session_id)
                log_debug(f"{LOG_PREFIX} Set initial idle animation for session {session_id}")
            else:
                log_debug(f"{LOG_PREFIX} Persona manager not available, skipping initial animation for session {session_id}")
        except Exception as anim_exc:
            log_warning(f"{LOG_PREFIX} Failed to set initial idle animation for session {session_id}: {anim_exc}")
        
        log_info(f"{LOG_PREFIX} Client connected: {session_id}")

        try:
            while True:
                data = await websocket.receive_text()
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    payload = {"text": data}
                text = (payload.get("text") or "").strip()
                if not text:
                    continue
                await self._append_history(session_id, "user", text)
                # Process message in background to avoid blocking WebSocket
                asyncio.create_task(self._handle_user_message(session_id, text))
        except WebSocketDisconnect:
            log_info(f"{LOG_PREFIX} Client disconnected: {session_id}")
        except Exception as exc:  # pragma: no cover - runtime issues
            log_error(f"{LOG_PREFIX} websocket error: {exc}")
        finally:
            self.connections.pop(session_id, None)
            self.message_history.pop(session_id, None)

    async def logs_ws_endpoint(self, websocket: WebSocket):  # pragma: no cover - runtime streaming
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

        log_debug(f"{LOG_PREFIX} Log file candidates: {[str(c) for c in unique_candidates]}")
        path = next((candidate for candidate in unique_candidates if candidate.exists()), unique_candidates[0])
        log_debug(f"{LOG_PREFIX} Selected log file: {path} (exists: {path.exists()})")

        try:
            # Prepare list of exception types that are considered 'normal' disconnects
            try:
                from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError
            except Exception:
                ConnectionClosedOK = ConnectionClosedError = None  # type: ignore
            try:
                from uvicorn.protocols.utils import ClientDisconnected
            except Exception:
                ClientDisconnected = None  # type: ignore
            from starlette.websockets import WebSocketDisconnect

            disconnect_exceptions = tuple(
                [exc for exc in (ConnectionClosedOK, ConnectionClosedError, ClientDisconnected, WebSocketDisconnect) if exc]
            )

            wait_seconds = self.log_wait_seconds if self.log_wait_seconds else 20
            waited = 0
            while not path.exists() and waited < wait_seconds:
                log_debug(f"{LOG_PREFIX} Waiting for log file... ({waited}/{wait_seconds}s)")
                await asyncio.sleep(1)
                waited += 1

            if not path.exists():
                error_msg = f"Log file not found: {path}"
                log_warning(f"{LOG_PREFIX} {error_msg}")
                try:
                    await websocket.send_text(error_msg)
                except Exception:
                    # Client probably disconnected before we could send
                    log_debug(f"{LOG_PREFIX} Client disconnected before receiving 'log not found' message")
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
                        if isinstance(exc, disconnect_exceptions) or isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                            log_info(f"{LOG_PREFIX} Log stream websocket disconnected while sending history: {type(exc).__name__}")
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
                        if isinstance(exc, disconnect_exceptions) or isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                            log_info(f"{LOG_PREFIX} Log stream websocket disconnected while streaming: {type(exc).__name__}")
                            return
                        import traceback
                        log_error(f"{LOG_PREFIX} log stream error: {exc}")
                        log_error(f"{LOG_PREFIX} Exception type: {type(exc).__name__}")
                        log_error(f"{LOG_PREFIX} Traceback: {traceback.format_exc()}")
                        try:
                            await websocket.send_text(f"--- log stream error: {exc} ---")
                        except Exception:
                            pass  # Websocket might be closed already
        finally:
            try:
                await websocket.close()
            except Exception:
                pass  # Websocket might already be closed

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
                "component": None
            }
        else:
            message = {
                "type": "action_state",
                "phase": state.get("phase"),
                "action_id": state.get("action_id"),
                "component": state.get("component")
            }
        
        log_info(f"{LOG_PREFIX} Broadcasting action state to {len(self.connections)} clients: {message['phase']}")
        
        # Send to all connected clients
        for session_id, websocket in self.connections.items():
            try:
                await websocket.send_json(message)
                log_info(f"{LOG_PREFIX} ✓ Sent action_state to session {session_id}: {message['phase']}")
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Failed to broadcast action state to session {session_id}: {exc}")

    async def _handle_user_message(self, session_id: str, text: str) -> None:
        from types import SimpleNamespace

        message = SimpleNamespace(
            chat_id=session_id,
            message_id=int(datetime.utcnow().timestamp() * 1000) % 1_000_000,
            text=text,
            date=datetime.utcnow(),
            from_user=SimpleNamespace(
                id=session_id,
                username=f"synth_{session_id[:8]}",
                first_name="SyntH",
                last_name="",
                full_name="SyntH User",
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
        
        # Global action ID for this message
        action_id = f"webui_msg_{session_id}_{message.message_id}"
        
        try:
            # Push THINKING action to global state
            log_info(f"{LOG_PREFIX} Pushing THINKING action: {action_id}")
            await self.action_state_manager.push_action(
                action_id=action_id,
                phase=AnimationPhase.THINKING,
                component="webui"
            )
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Failed to push action state: {exc}")
        
        # Get the configured response timeout from message_chain
        from core.message_chain import RESPONSE_TIMEOUT
        timeout_seconds = int(RESPONSE_TIMEOUT)
        
        try:
            # Call plugin to handle message
            response = await asyncio.wait_for(
                plugin_instance.handle_incoming_message(
                    self, message, {}, INTERFACE_NAME
                ),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            log_error(f"{LOG_PREFIX} Message handling timed out after {timeout_seconds}s for session {session_id}")
            response = str(get_failed_message_text())
        except Exception as exc:  # pragma: no cover - runtime issues
            log_error(f"{LOG_PREFIX} error handling message: {exc}")
            response = str(get_failed_message_text())

        # Ensure we keep the THINKING action active until we've delivered the response
        if response:
            # Push WRITING so clients can display write animation while we deliver the message
            writing_action_id = f"webui_write_{session_id}_{int(datetime.utcnow().timestamp() * 1000) % 1_000_000}"
            try:
                await self.action_state_manager.push_action(
                    action_id=writing_action_id,
                    phase=AnimationPhase.WRITING,
                    component="webui"
                )
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Failed to push WRITING action state: {exc}")

            try:
                await self.send_message(session_id, text=response)
            except Exception as send_exc:
                log_warning(f"{LOG_PREFIX} Failed to send response to session {session_id}: {send_exc}")
            finally:
                # Pop WRITING first, then THINKING
                try:
                    await self.action_state_manager.pop_action(writing_action_id)
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} Failed to pop WRITING action state: {exc}")
                try:
                    await self.action_state_manager.pop_action(action_id)
                    log_info(f"{LOG_PREFIX} Popped action: {action_id} - returning to IDLE")
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} Failed to pop THINKING action state: {exc}")
        else:
            # Some LLM engines return None but process asynchronously; keep THINKING for a short grace period
            async def delayed_pop():
                await asyncio.sleep(0.5)
                try:
                    await self.action_state_manager.pop_action(action_id)
                    log_info(f"{LOG_PREFIX} Delayed pop action: {action_id} - returning to IDLE")
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} Failed to delayed pop action state: {exc}")

            try:
                asyncio.create_task(delayed_pop())
            except Exception:
                # Fallback to immediate pop if task creation fails
                try:
                    await self.action_state_manager.pop_action(action_id)
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} Failed to pop action state after fallback: {exc}")

    async def _replay_history(self, session_id: str) -> None:
        history = self.message_history.get(session_id)
        if not history:
            return
        websocket = self.connections.get(session_id)
        if not websocket:
            return
        for item in history:
            await websocket.send_json({"type": "message", **item})

    async def _append_history(self, session_id: str, sender: str, text: str) -> None:
        history = self.message_history.setdefault(
            session_id, deque(maxlen=self.max_history)
        )
        history.append({"sender": sender, "text": text})

    # ------------------------------------------------------------------
    # Methods used by actions / plugins
    # ------------------------------------------------------------------
    async def send_message(
        self,
        payload_or_chat_id=None,
        text: Optional[str] = None,
        **kwargs,
    ) -> None:
        if isinstance(payload_or_chat_id, dict):
            payload = payload_or_chat_id
            text = payload.get("text", text)
            chat_id = payload.get("target") or payload.get("chat_id")
        else:
            chat_id = payload_or_chat_id or kwargs.get("chat_id")
            if text is None:
                text = kwargs.get("text")

        if not text or not chat_id:
            log_warning(f"{LOG_PREFIX} send_message missing text or chat_id")
            return

        websocket = self.connections.get(str(chat_id))
        if not websocket:
            # Improved debug information: list active sessions to help debug target mismatches
            active_sessions = list(self.connections.keys())
            log_warning(f"{LOG_PREFIX} no active websocket for session {chat_id}. Active sessions: {active_sessions}")
            log_debug(f"{LOG_PREFIX} send_message payload target: {chat_id}, text length: {len(text) if text else 0}")
            return

        await websocket.send_json({"type": "message", "sender": "synth", "text": text})
        await self._append_history(str(chat_id), "synth", text)
        log_info(f"{LOG_PREFIX} Sent message to session {chat_id}: {text[:80]}{'...' if len(text)>80 else ''}")

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        if action.get("type") == "message_synth_webui":
            payload = action.get("payload", {})
            await self.send_message(payload, original_message=original_message)

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
                    log_info(f"{LOG_PREFIX} Active VRM loaded from marker: {candidate.name}")
                    return candidate.name
                else:
                    log_warning(f"{LOG_PREFIX} Marker references non-existent file: {candidate}")
        else:
            log_debug(f"{LOG_PREFIX} No marker file found, looking for first available VRM...")
        
        # Fallback to first available model, preferring SynTh.vrm as default
        available_vrms = list(sorted(self.vrm_dir.glob("*.vrm")))
        log_debug(f"{LOG_PREFIX} Available VRM files: {[v.name for v in available_vrms]}")
        
        # Prefer SyntH.vrm as the default model
        synth_vrm = self.vrm_dir / "SyntH.vrm"
        if synth_vrm.exists():
            log_info(f"{LOG_PREFIX} Using default SyntH.vrm model")
            self._set_active_vrm(synth_vrm.name)
            return synth_vrm.name
        
        # Otherwise use first available
        for candidate in available_vrms:
            log_info(f"{LOG_PREFIX} Using first available VRM: {candidate.name}")
            return candidate.name
        
        log_warning(f"{LOG_PREFIX} No VRM models found in directory")
        return None

    def _set_active_vrm(self, model_name: Optional[str]) -> None:
        log_info(f"{LOG_PREFIX} ========== SET ACTIVE VRM START ==========")
        log_info(f"{LOG_PREFIX} Setting active VRM to: '{model_name}'")
        log_debug(f"{LOG_PREFIX} Current active VRM before change: '{self.active_vrm}'")
        log_debug(f"{LOG_PREFIX} Active VRM marker path: {self.active_vrm_marker}")
        log_debug(f"{LOG_PREFIX} Active VRM marker exists: {self.active_vrm_marker.exists() if hasattr(self, 'active_vrm_marker') else 'N/A'}")
        
        if not model_name:
            log_info(f"{LOG_PREFIX} Clearing active VRM (model_name is None/empty)")
            try:
                if self.active_vrm_marker.exists():
                    log_debug(f"{LOG_PREFIX} Removing active VRM marker file...")
                    self.active_vrm_marker.unlink()
                    log_info(f"{LOG_PREFIX} ✓ Removed active VRM marker")
                else:
                    log_debug(f"{LOG_PREFIX} Active VRM marker does not exist, nothing to remove")
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
                        log_error(f"{LOG_PREFIX}   - {item.name} ({'file' if item.is_file() else 'dir'})")
                    if not contents:
                        log_error(f"{LOG_PREFIX}   (directory is empty)")
                else:
                    log_error(f"{LOG_PREFIX}   (directory does not exist)")
            except Exception as list_exc:
                log_error(f"{LOG_PREFIX} Failed to list directory: {list_exc}")
            
            log_info(f"{LOG_PREFIX} ========== SET ACTIVE VRM END (not found) ==========")
            raise FileNotFoundError(model_name)
            
        log_debug(f"{LOG_PREFIX} ✓ VRM file exists, writing marker...")
        log_debug(f"{LOG_PREFIX} Marker will contain: '{candidate.name}'")
        
        try:
            self.active_vrm_marker.write_text(candidate.name, encoding="utf-8")
            log_info(f"{LOG_PREFIX} ✓ Wrote marker file for: {candidate.name}")
            log_debug(f"{LOG_PREFIX} Marker file exists after write: {self.active_vrm_marker.exists()}")
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


    @staticmethod
    def _sanitize_vrm_filename(name: str) -> str:
        stem = Path(name or "avatar").stem
        safe = "".join(ch for ch in stem if ch.isalnum() or ch in ("-", "_")).strip("_-")
        if not safe:
            safe = "avatar"
        return f"{safe}_{uuid.uuid4().hex[:8]}.vrm"

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
        definitions = config_registry.export_definitions()
        items = []
        for entry in definitions:
            # Skip bootstrap-tagged items (not meant for UI)
            if "bootstrap" in entry.get("tags", []):
                continue
            component_label = self._get_display_name(entry["component"], None)
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
                    "advanced": entry["advanced"],
                    "sensitive": entry["sensitive"],
                    "env_override": entry["env_override"],
                    "value_type": entry["value_type"],
                    "editable": not entry["env_override"],
                    "constraints": entry.get("constraints"),
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

    async def get_selkies_config(self):
        """Return Selkies configuration for dynamic URL construction."""
        return JSONResponse({
            "https_port": self.selkies_https_port,
            "http_port": self.selkies_http_port
        })

    async def get_animations_for_type(self, skin: str, animation_type: str):
        """Return list of animation files for a specific skin and animation type.
        
        Example: GET /api/animations/Rei/idle
        Returns: {"animations": ["Idle.fbx", "Idle2.fbx", "Look Around.fbx"]}
        """
        try:
            # Validate skin and animation_type to prevent directory traversal
            if ".." in skin or ".." in animation_type:
                raise HTTPException(status_code=400, detail="Invalid skin or animation type")
            
            anim_dir = Path(__file__).parent.parent / "skins" / skin / "animations" / animation_type
            
            if not anim_dir.exists():
                log_debug(f"{LOG_PREFIX} Animation directory not found: {anim_dir}")
                return JSONResponse({"animations": []})
            
            # Get all .fbx files in the directory (non-recursive, ignore subdirectories)
            fbx_files = sorted([
                f.name for f in anim_dir.iterdir() 
                if f.is_file() and f.suffix.lower() == '.fbx'
            ])
            
            log_debug(f"{LOG_PREFIX} Found {len(fbx_files)} animations in {skin}/{animation_type}: {fbx_files}")
            return JSONResponse({"animations": fbx_files})
        except Exception as e:
            log_error(f"{LOG_PREFIX} Error listing animations for {skin}/{animation_type}: {e}")
            return JSONResponse({"animations": []}, status_code=500)

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
            max_chars = _bounded_int(max_chars_param, default=20000, minimum=1000, maximum=200000)
        else:
            max_chars = 20000
        include_archived = params.get("include_archived", "false").lower() == "true"
        
        # Pagination parameters
        page = _bounded_int(params.get("page"), default=1, minimum=1, maximum=1000)
        per_page = _bounded_int(params.get("per_page"), default=10, minimum=1, maximum=1000)

        persona_snapshot = await self._fetch_persona_snapshot()
        diary_payload = await self._fetch_diary_entries(days=days, limit=limit, max_chars=max_chars, include_archived=include_archived, page=page, per_page=per_page)

        if not persona_snapshot.get("created_at") and diary_payload.get("earliest_timestamp"):
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
        return JSONResponse(response)

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
                        log_debug(f"{LOG_PREFIX} Persona async_init failed: {async_exc}")
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

    async def _fetch_diary_entries(self, *, days: int, limit: int, max_chars: int, include_archived: bool = False, page: int = 1, per_page: int = 10) -> Dict[str, Any]:
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
            from core.db import get_conn
            
            conn = await get_conn()
            try:
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
            finally:
                conn.close()
            
            payload["total_count"] = total_count
            payload["total_pages"] = (total_count + per_page - 1) // per_page if per_page != 'unlimited' else 1
            
            # Calculate offset
            if per_page == 'unlimited':
                offset = 0
                limit = total_count
            else:
                offset = (page - 1) * per_page
                limit = per_page
            
            # Fetch paginated entries
            conn = await get_conn()
            try:
                async with conn.cursor() as cur:
                    if include_archived:
                        # Get entries from both tables, ordered by timestamp DESC
                        await cur.execute("""
                            (SELECT id, content, personal_thought, timestamp, context_tags, involved_users, 
                                   emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                                   FALSE as archived
                            FROM ai_diary)
                            UNION ALL
                            (SELECT id, content, personal_thought, timestamp, context_tags, involved_users, 
                                   emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                                   TRUE as archived
                            FROM ai_diary_archive)
                            ORDER BY timestamp DESC
                            LIMIT %s OFFSET %s
                        """, (limit, offset))
                    else:
                        await cur.execute("""
                            SELECT id, content, personal_thought, timestamp, context_tags, involved_users, 
                                   emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                                   FALSE as archived
                            FROM ai_diary
                            ORDER BY timestamp DESC
                            LIMIT %s OFFSET %s
                        """, (limit, offset))
                    
                    rows = await cur.fetchall()
            finally:
                conn.close()
            
            # Convert rows to entries format
            entries = []
            for row in rows:
                entry = {
                    'id': row[0],
                    'content': row[1],
                    'personal_thought': row[2],
                    'timestamp': row[3].isoformat() if row[3] else None,
                    'context_tags': json.loads(row[4] or '[]'),
                    'involved_users': json.loads(row[5] or '[]'),
                    'emotions': json.loads(row[6] or '[]'),
                    'interface': row[7],
                    'chat_id': row[8],
                    'thread_id': row[9],
                    'interaction_summary': row[10],
                    'user_message': row[11],
                    'archived': row[12]
                }
                entries.append(entry)
            
            payload["entries"] = entries
            payload["count"] = len(entries)
            payload["available"] = plugin_enabled and bool(total_count)
            
            # Calculate timestamps from current page (not all entries)
            timestamps = [entry.get("timestamp") for entry in entries if entry.get("timestamp")]
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
                return JSONResponse({"success": True, "archived_count": result.get("archived_count", 0)})
            else:
                raise HTTPException(status_code=500, detail=result.get("error", "Archive failed"))
                
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
                return JSONResponse({"success": True, "unarchived_count": result.get("unarchived_count", 0)})
            else:
                raise HTTPException(status_code=500, detail=result.get("error", "Unarchive failed"))
                
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
                return JSONResponse({"success": True, "deleted_count": result.get("deleted_count", 0)})
            else:
                raise HTTPException(status_code=500, detail=result.get("error", "Delete failed"))
                
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to delete archived diary entries: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))

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
            log_error(f"{LOG_PREFIX} 'value' not in payload. Keys: {list(payload.keys())}")
            log_error(f"{LOG_PREFIX} Full payload: {payload}")
            raise HTTPException(status_code=400, detail="Missing configuration value")

        value = payload.get("value")
        log_debug(f"{LOG_PREFIX} Updating config: key={key}, value_type={type(value)}, value_len={len(str(value)) if value else 0}")
        
        # Get component info before updating
        try:
            definitions = config_registry.export_definitions()
            config_def = next((d for d in definitions if d["key"] == key), None)
            component = config_def.get("component") if config_def else None
        except Exception:
            component = None
        
        try:
            await config_registry.set_value(key, value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            log_error(f"{LOG_PREFIX} failed to update config {key}: {exc}")
            raise HTTPException(status_code=500, detail="Failed to update configuration") from exc

        response_data = {"status": "ok"}
        
        # Check if component reload is needed. Prefer an explicit flag coming
        # from the config definition (needs_component_reload). This avoids
        # suggesting reloads for synthetic components like 'exposed' unless a
        # variable explicitly declared that changing it requires a reload.
        try:
            needs_reload_flag = bool(config_def.get("needs_component_reload", False)) if config_def else False
        except Exception:
            needs_reload_flag = False

        if component and component not in ["core", "webui"] and needs_reload_flag:
            response_data["requires_reload"] = True
            response_data["component"] = component
            response_data["message"] = f"Configuration updated. Component '{component}' should be reloaded for changes to take effect."
            log_warning(f"{LOG_PREFIX} Config '{key}' for component '{component}' changed - component reload recommended")
        
        return JSONResponse(response_data)

    async def get_logchat_info(self):
        """Return LogChat configuration status."""
        try:
            from core.config import get_log_chat_id, get_log_chat_interface
            log_chat_id = await get_log_chat_id()
            log_chat_interface = await get_log_chat_interface()
            
            if log_chat_id and log_chat_interface:
                return JSONResponse({
                    "configured": True,
                    "interface": log_chat_interface,
                    "chat_id": str(log_chat_id)
                })
            return JSONResponse({"configured": False})
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to get logchat info: {exc}")
            return JSONResponse({"configured": False, "error": str(exc)})

    async def get_active_vrm_endpoint(self):
        log_debug(f"{LOG_PREFIX} Getting active VRM: {self.active_vrm}")
        if self.active_vrm:
            # Build URL relative to web root based on vrm_dir
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
        # Preload the current idle animation to all connected clients so the newly-loaded
        # VRM does not appear in a T-pose while the client initializes the model.
        try:
            if self.persona_manager:
                for session in list(self.connections.keys()):
                    try:
                        # persona_manager.set_animation_state accepts the state name and session_id
                        await self.persona_manager.set_animation_state("idle", session_id=session)
                        log_debug(f"{LOG_PREFIX} Preloaded idle animation for session {session}")
                    except Exception as anim_exc:
                        log_warning(f"{LOG_PREFIX} Failed to preload idle for session {session}: {anim_exc}")
            else:
                log_debug(f"{LOG_PREFIX} Persona manager not available - skipping idle preload for connected clients")
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Error while preloading idle animations: {exc}")
        return JSONResponse(
            {"status": "ok", "name": candidate.name, "url": f"/avatars/{candidate.name}"}
        )

    async def upload_vrm_model(self, file: UploadFile = File(...)):
        log_info(f"{LOG_PREFIX} ========== VRM UPLOAD START ==========")
        log_info(f"{LOG_PREFIX} VRM upload started: {file.filename if file else 'no file'}")
        log_debug(f"{LOG_PREFIX} File content type: {file.content_type if file else 'N/A'}")
        log_debug(f"{LOG_PREFIX} File size (from file object): {file.size if hasattr(file, 'size') else 'unknown'}")
        log_debug(f"{LOG_PREFIX} VRM directory: {self.vrm_dir}")
        log_debug(f"{LOG_PREFIX} VRM directory exists: {self.vrm_dir.exists()}")
        log_debug(f"{LOG_PREFIX} VRM directory is_dir: {self.vrm_dir.is_dir() if self.vrm_dir.exists() else 'N/A'}")
        
        if not file or not file.filename:
            log_warning(f"{LOG_PREFIX} VRM upload failed: no file provided")
            raise HTTPException(status_code=400, detail="No file uploaded")
        
        log_debug(f"{LOG_PREFIX} Original filename: '{file.filename}'")
        
        if not file.filename.lower().endswith(".vrm"):
            log_warning(f"{LOG_PREFIX} VRM upload failed: invalid extension for {file.filename}")
            raise HTTPException(status_code=400, detail="Only .vrm files are accepted")
        
        filename = self._sanitize_vrm_filename(file.filename)
        log_info(f"{LOG_PREFIX} Sanitized filename: '{filename}'")
        
        destination = self.vrm_dir / filename
        log_info(f"{LOG_PREFIX} Full destination path: {destination}")
        log_debug(f"{LOG_PREFIX} Destination parent exists: {destination.parent.exists()}")
        log_debug(f"{LOG_PREFIX} Destination parent is writable: {os.access(destination.parent, os.W_OK) if destination.parent.exists() else 'N/A'}")
        
        try:
            log_debug(f"{LOG_PREFIX} Opening destination file for writing...")
            # Per new behavior, always write to model.vrm inside the VRM dir (overwrite)
            destination = self.vrm_dir / "model.vrm"
            with destination.open("wb") as buffer:
                log_debug(f"{LOG_PREFIX} File opened successfully, starting to read chunks...")
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
                        log_debug(f"{LOG_PREFIX} Written {bytes_written} bytes so far...")
                        
                log_info(f"{LOG_PREFIX} VRM upload complete: {filename} ({bytes_written} bytes, {chunk_count} chunks)")
                log_debug(f"{LOG_PREFIX} File exists after write: {destination.exists()}")
                log_debug(f"{LOG_PREFIX} File size on disk: {destination.stat().st_size if destination.exists() else 'N/A'}")
                
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
                    log_error(f"{LOG_PREFIX} Failed to cleanup partial upload: {cleanup_exc}")
            raise HTTPException(status_code=500, detail="Failed to store VRM file")
        finally:
            await file.close()
            log_debug(f"{LOG_PREFIX} File handle closed")

        log_info(f"{LOG_PREFIX} Setting active VRM to: model.vrm")
        # Persist marker pointing to model.vrm
        try:
            self._set_active_vrm("model.vrm")
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Failed to persist active VRM marker: {exc}")
        log_info(f"{LOG_PREFIX} Active VRM set successfully")
        
        response_data = {"status": "ok", "name": "model.vrm", "url": f"/avatars/model.vrm"}
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

    async def upload_persona_pack(self, file: UploadFile = File(None), folder_path: Optional[str] = None):
        """Upload a persona pack (.zip or .shp) containing a VRM, animations, descriptor and preview image.

        The pack will be extracted into res/synth_webui/personas/<name> and the VRM will be copied into /avatars.
        """
        import zipfile
        personas_dir = Path(__file__).resolve().parent.parent / "res" / "synth_webui" / "personas"
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
                raise HTTPException(status_code=400, detail="Provided folder_path does not exist or is not a directory")
            # Create a unique dest folder name based on folder basename
            root = src.name
            dest = personas_dir / root
            if dest.exists():
                dest = personas_dir / f"{root}_{uuid.uuid4().hex[:6]}"
            import shutil
            try:
                shutil.copytree(src, dest)
            except Exception as exc:
                log_error(f"{LOG_PREFIX} Failed to copy persona folder from {src} to {dest}: {exc}")
                raise HTTPException(status_code=500, detail="Failed to copy persona folder")
        else:
            if not file or not file.filename:
                raise HTTPException(status_code=400, detail="No file uploaded")

            filename = Path(file.filename).name
            lower = filename.lower()
            if not (lower.endswith('.zip') or lower.endswith('.shp')):
                raise HTTPException(status_code=400, detail="Only .zip or .shp persona packs are accepted")

            # Save uploaded archive to a temp location
            temp_path = personas_dir / f"upload_{uuid.uuid4().hex}.tmp"
            try:
                with temp_path.open('wb') as f:
                    while True:
                        chunk = await file.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
            finally:
                await file.close()

            # Extract
            try:
                with zipfile.ZipFile(temp_path, 'r') as zf:
                    # Determine root folder name from archive (or use filename sans ext)
                    root_candidates = [n.split('/')[0] for n in zf.namelist() if n and '/' in n]
                    root = root_candidates[0] if root_candidates else Path(filename).stem
                    dest = personas_dir / root
                    if dest.exists():
                        # create unique folder
                        dest = personas_dir / f"{root}_{uuid.uuid4().hex[:6]}"
                    dest.mkdir(parents=True, exist_ok=True)
                    zf.extractall(dest)

                # Find a .vrm file inside dest
                vrm_file = None
                for p in dest.rglob('*.vrm'):
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
                    animations_src = dest / 'animations'
                    animations_dest = Path(__file__).resolve().parent.parent / 'res' / 'synth_webui' / 'animations'
                    if animations_src.exists() and animations_src.is_dir():
                        animations_dest.mkdir(parents=True, exist_ok=True)
                        for anim in animations_src.iterdir():
                            try:
                                shutil.copy2(anim, animations_dest / anim.name)
                            except Exception:
                                pass

                    # If there's a persona metadata file, try to read name/preview
                    meta = None
                    for m in dest.glob('*.md'):
                        try:
                            meta = m.read_text(encoding='utf-8')
                            break
                        except Exception:
                            continue

                    # Mark this VRM as active (optional - for now set as active)
                    self._set_active_vrm(target.name)

                    return JSONResponse({
                        'status': 'ok',
                        'name': target.name,
                        'skin_folder': str(dest),
                        'meta': meta,
                    }, status_code=201)
                else:
                    return JSONResponse({'status': 'error', 'detail': 'No VRM found in persona pack'}, status_code=400)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail='Invalid zip file')
            except Exception as exc:
                log_error(f"{LOG_PREFIX} Failed to process persona pack: {exc}")
                raise HTTPException(status_code=500, detail='Failed to process persona pack')
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
            "synth_webui": "SyntH Web UI",
            "synth-webui": "SyntH Web UI",
            "synth_webui_interface": "SyntH Web UI",
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
        return " ".join(part.capitalize() if part.upper() != part else part for part in cleaned.split())

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
            if interface and hasattr(interface, 'is_enabled') and not interface.is_enabled:
                reason = getattr(interface, 'disabled_reason', 'Disabled')
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
            from core.core_initializer import PLUGIN_REGISTRY, INTERFACE_REGISTRY, core_initializer
            from core.llm_registry import get_llm_registry
            from core.config import list_available_llms, get_active_llm
        except Exception as exc:  # pragma: no cover - defensive
            log_error(f"{LOG_PREFIX} component inspection import failure: {exc}")
            raise HTTPException(status_code=500, detail="Unable to inspect components") from exc

        available_llms = []
        try:
            available_llms = list_available_llms()
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} unable to list available LLMs: {exc}")

        try:
            active_llm = await get_active_llm()
        except Exception as exc:
            log_error(f"{LOG_PREFIX} unable to resolve active LLM: {exc}")
            active_llm = None

        llm_registry = get_llm_registry()
        engine_names = set()
        try:
            engine_names.update(llm_registry.get_available_engines())
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} unable to list registered LLM engines: {exc}")
        engine_names.update(available_llms)
        if active_llm:
            engine_names.add(active_llm)

        llm_engines: List[dict] = []
        for engine_name in sorted(engine_names):
            instance = None
            try:
                instance = llm_registry.get_engine(engine_name)
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} unable to retrieve engine {engine_name}: {exc}")
            actions = []
            if instance and hasattr(instance, "get_supported_actions"):
                try:
                    actions = self._format_actions(instance.get_supported_actions())
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} error reading actions for engine {engine_name}: {exc}")
            elif instance and hasattr(instance, "get_supported_action_types"):
                try:
                    actions = self._format_actions(instance.get_supported_action_types())
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} error reading action types for engine {engine_name}: {exc}")

            meta = self._get_component_meta(engine_name)
            llm_engines.append(
                {
                    "name": engine_name,
                    "display_name": self._get_display_name(engine_name, instance),
                    "active": engine_name == active_llm,
                    "loaded": instance is not None,
                    "description": self._extract_description(instance),
                    "status": meta["status"],
                    "details": meta["details"],
                    "error": meta["error"],
                    "actions": actions,
                }
            )

        interfaces_data: List[dict] = []
        for name, interface in sorted(INTERFACE_REGISTRY.items()):
            description = ""
            if hasattr(interface, "get_interface_instructions"):
                try:
                    description = interface.get_interface_instructions() or ""
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} interface instruction retrieval failed for {name}: {exc}")
            if not description:
                description = self._extract_description(interface)

            actions = []
            if hasattr(interface, "get_supported_actions"):
                try:
                    actions = self._format_actions(interface.get_supported_actions())
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} interface action retrieval failed for {name}: {exc}")
            elif hasattr(interface, "get_supported_action_types"):
                try:
                    actions = self._format_actions(interface.get_supported_action_types())
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} interface action type retrieval failed for {name}: {exc}")

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

        # Add Selkies Web Desktop as a special hardcoded component
        # Use SELKIES_HTTPS_PORT (default 3000) for HTTPS connections
        # Use SELKIES_HTTP_PORT (default 3001) for HTTP connections
        # Note: The actual hostname will be resolved client-side in JavaScript
        selkies_protocol = "https" if os.getenv("SECURE_CONNECTION", "0") == "1" else "http"
        selkies_port = self.selkies_https_port if selkies_protocol == "https" else self.selkies_http_port
        
        # Mark as dynamic - JavaScript will construct the full URL client-side
        interfaces_data.append(
            {
                "name": "selkies_desktop",
                "display_name": "Selkies Web Desktop",
                "description": "Web-based VNC desktop environment for visual interaction with the SyntH container. Provides full desktop access with Chrome browser.",
                "actions": [],
                "status": "success",
                "details": f"Available at {selkies_protocol}://[host]:{selkies_port}",
                "error": None,
                "url": None,  # Will be set client-side
                "is_external": True,
                "selkies_protocol": selkies_protocol,
                "selkies_port": selkies_port,
            }
        )

        plugins_data: List[dict] = []
        for name, plugin in sorted(PLUGIN_REGISTRY.items()):
            description = self._extract_description(plugin)
            actions = []
            if hasattr(plugin, "get_supported_actions"):
                try:
                    actions = self._format_actions(plugin.get_supported_actions())
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} plugin action retrieval failed for {name}: {exc}")
            elif hasattr(plugin, "get_supported_action_types"):
                try:
                    actions = self._format_actions(plugin.get_supported_action_types())
                except Exception as exc:
                    log_warning(f"{LOG_PREFIX} plugin action type retrieval failed for {name}: {exc}")

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

        payload = {
            "llm": {
                "active": active_llm,
                "available": available_llms,
                "engines": llm_engines,
            },
            "interfaces": interfaces_data,
            "plugins": plugins_data,
            "summary": component_summary,
            "dev_components_enabled": dev_components_enabled,
        }
        return JSONResponse(payload)

    async def set_llm_engine(self, request: Request):
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        name = str(data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Missing 'name'")

        try:
            from core.config import list_available_llms, set_active_llm
            from core.plugin_instance import load_plugin
        except Exception as exc:  # pragma: no cover - defensive
            log_error(f"{LOG_PREFIX} unable to import LLM configuration helpers: {exc}")
            raise HTTPException(status_code=500, detail="Unable to access LLM configuration") from exc

        available = list_available_llms()
        if name not in available:
            raise HTTPException(status_code=404, detail=f"LLM '{name}' is not available")

        try:
            # Hot-swap: Just load the new plugin without full restart
            await set_active_llm(name)
            await load_plugin(name)
            log_info(f"{LOG_PREFIX} Successfully switched LLM to {name}")
        except Exception as exc:
            log_error(f"{LOG_PREFIX} failed to switch LLM to {name}: {exc}")
            raise HTTPException(status_code=500, detail=f"Failed to activate LLM '{name}'") from exc

        return JSONResponse({"status": "ok", "active": name})

    async def reload_component(self, request: Request):
        """Reload a specific component (interface or plugin)."""
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        component_type = str(data.get("type") or "").strip().lower()
        component_name = str(data.get("name") or "").strip()

        if not component_type or component_type not in ["interface", "plugin", "llm"]:
            raise HTTPException(status_code=400, detail="Missing or invalid 'type'. Must be 'interface', 'plugin', or 'llm'")
        
        if not component_name:
            raise HTTPException(status_code=400, detail="Missing 'name'")

        try:
            from core.core_initializer import PLUGIN_REGISTRY, INTERFACE_REGISTRY
            from core.llm_registry import get_llm_registry
        except Exception as exc:
            log_error(f"{LOG_PREFIX} unable to import registries: {exc}")
            raise HTTPException(status_code=500, detail="Unable to access component registries") from exc

        try:
            if component_type == "interface":
                # Reload interface
                interface_instance = INTERFACE_REGISTRY.get(component_name)
                if not interface_instance:
                    raise HTTPException(status_code=404, detail=f"Interface '{component_name}' not found")
                
                # Stop if running
                if hasattr(interface_instance, 'stop'):
                    log_info(f"{LOG_PREFIX} Stopping interface '{component_name}'...")
                    try:
                        await interface_instance.stop()
                    except Exception as stop_exc:
                        log_warning(f"{LOG_PREFIX} Error stopping interface '{component_name}': {stop_exc}")
                
                # Start again
                if hasattr(interface_instance, 'start'):
                    log_info(f"{LOG_PREFIX} Starting interface '{component_name}'...")
                    await interface_instance.start()
                else:
                    log_warning(f"{LOG_PREFIX} Interface '{component_name}' has no start() method")
                
                log_info(f"{LOG_PREFIX} Interface '{component_name}' reloaded successfully")
                return JSONResponse({"status": "ok", "message": f"Interface '{component_name}' reloaded successfully"})
            
            elif component_type == "plugin":
                # Reload plugin
                plugin_instance = PLUGIN_REGISTRY.get(component_name)
                if not plugin_instance:
                    raise HTTPException(status_code=404, detail=f"Plugin '{component_name}' not found")
                
                # Plugins typically don't need reload, but we can report success
                log_info(f"{LOG_PREFIX} Plugin '{component_name}' noted for reload (plugins use ConfigVar auto-updates)")
                return JSONResponse({"status": "ok", "message": f"Plugin '{component_name}' configuration updated"})
            
            elif component_type == "llm":
                # Reload LLM engine
                llm_registry = get_llm_registry()
                
                # Check if engine exists
                if component_name not in llm_registry.get_available_engines():
                    raise HTTPException(status_code=404, detail=f"LLM engine '{component_name}' not found")
                
                # Unload current instance if exists
                current_instance = llm_registry.get_engine(component_name)
                if current_instance:
                    log_info(f"{LOG_PREFIX} Unloading LLM engine '{component_name}'...")
                    llm_registry.unload_engine(component_name)
                
                # Reload the engine
                log_info(f"{LOG_PREFIX} Reloading LLM engine '{component_name}'...")
                try:
                    new_instance = llm_registry.load_engine(component_name)
                    log_info(f"{LOG_PREFIX} LLM engine '{component_name}' reloaded successfully")
                    return JSONResponse({"status": "ok", "message": f"LLM engine '{component_name}' reloaded successfully"})
                except Exception as load_exc:
                    log_error(f"{LOG_PREFIX} Failed to reload LLM engine '{component_name}': {load_exc}")
                    raise HTTPException(status_code=500, detail=f"Failed to reload LLM engine '{component_name}': {str(load_exc)}") from load_exc
        
        except HTTPException:
            raise
        except Exception as exc:
            log_error(f"{LOG_PREFIX} failed to reload {component_type} '{component_name}': {exc}")
            raise HTTPException(status_code=500, detail=f"Failed to reload {component_type} '{component_name}': {str(exc)}") from exc

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
            log_info(f"{LOG_PREFIX} Dev components {status_msg} globally (will persist across restarts)")
            
            # Note: This does NOT automatically reload components - user must restart
            return JSONResponse({
                "status": "ok",
                "enabled": enabled,
                "message": f"Dev components {status_msg}. Restart required to apply changes."
            })
        
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to toggle dev components: {exc}")
            raise HTTPException(status_code=500, detail=f"Failed to toggle dev components: {str(exc)}") from exc

    async def restart_system(self, request: Request):
        """Restart the entire SyntH system by triggering the restart mechanism."""
        try:
            log_info(f"{LOG_PREFIX} System restart requested via API")
            
            # Send response before restarting
            response = JSONResponse({
                "status": "ok",
                "message": "SyntH is restarting... This may take a few moments."
            })
            
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
            raise HTTPException(status_code=500, detail=f"Failed to restart system: {str(exc)}") from exc

    def start_server_async(self) -> None:
        """Start the web server as an asyncio task. Call this from the main event loop."""
        try:
            if not hasattr(self, '_server_task') or (hasattr(self, '_server_task') and (self._server_task is None or self._server_task.done())):
                log_info(f"{LOG_PREFIX} Starting {BRAND_NAME} server as asyncio task on http://{self.host}:{self.port}")
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    self._server_task = loop.create_task(self._run_server())
                    log_info(f"{LOG_PREFIX} Server task scheduled on running loop: {loop}")
                except RuntimeError:
                    # No running loop - fallback to create_task which may raise later
                    log_warning(f"{LOG_PREFIX} No running event loop found when scheduling server task; attempting asyncio.create_task fallback")
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
            log_info(f"{LOG_PREFIX} Creating Uvicorn config for http://{self.host}:{self.port}")
            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                log_level=self.log_level or "info",
                lifespan="off",
            )
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

    async def start(self) -> None:
        """Start the web UI interface if autostart is enabled."""
        try:
            log_info(f"{LOG_PREFIX} start() called - initializing persona manager and starting server if enabled")
            # Initialize persona manager now that core initialization is complete
            if self.persona_manager is None:
                self.persona_manager = get_persona_manager()
                if self.persona_manager:
                    try:
                        self.persona_manager.set_webui(self)
                        self.persona_manager.set_animation_handler(self.animation_handler)
                    except Exception as pm_exc:
                        log_warning(f"{LOG_PREFIX} Persona manager set_* calls failed: {pm_exc}")
                    log_info(f"{LOG_PREFIX} Persona manager initialized")
                else:
                    log_warning(f"{LOG_PREFIX} Failed to initialize persona manager")

            if self.autostart:
                log_info(f"{LOG_PREFIX} Autostart enabled, starting {BRAND_NAME} server")
                try:
                    self.start_server_async()
                    log_info(f"{LOG_PREFIX} start() completed - server start scheduled")
                except Exception as start_exc:
                    import traceback
                    log_error(f"{LOG_PREFIX} Exception while invoking start_server_async: {start_exc}")
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
        return template.replace('{brand_name}', BRAND_NAME)

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
                <label><input type="checkbox" id="show-archived" /> Show archived</label>
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
                const showArchived = document.getElementById('show-archived').checked;
                const response = await fetch(`/api/diary?days=365&limit=1000&include_archived=${{showArchived}}`);
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
        return template.replace('{brand_name}', BRAND_NAME)

    async def list_skins(self):
        """List available skins (folders under skins).

        Returns: JSON list with entries: name, preview_url (if present), meta (persona.md text), vrm_present
        """
        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        result = []
        if not skins_dir.exists():
            return JSONResponse(result)

        for entry in sorted(skins_dir.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name == 'temp':
                continue
            preview = None
            meta = None
            vrm_present = False
            preview_path = entry / 'preview.png'
            if preview_path.exists():
                preview = f"/skins/{name}/preview.png"
            # read persona md if present
            for m in entry.glob('*.md'):
                try:
                    meta = m.read_text(encoding='utf-8')
                    break
                except Exception:
                    continue
            # check for vrm
            for v in entry.rglob('*.vrm'):
                vrm_present = True
                break
            result.append({
                'name': name,
                'preview_url': preview,
                'meta': meta,
                'vrm_present': vrm_present,
                'valid': vrm_present,
            })

        # Ensure Rei exists and is valid
        rei = next((s for s in result if s['name'] == 'Rei'), None)
        if not rei:
            raise HTTPException(status_code=500, detail="Default skin 'Rei' missing")
        if not rei.get('valid'):
            raise HTTPException(status_code=500, detail="Default skin 'Rei' invalid (missing VRM)")

        return JSONResponse(result)

    async def clear_uploaded_vrm(self):
        """Clear any user-uploaded VRM in skins/temp/model.vrm and restore Rei's VRM.

        This sets the active VRM to the restored model.
        """
        skins_dir = Path(__file__).resolve().parent.parent / "skins"
        rei_dir = skins_dir / 'Rei'
        if not rei_dir.exists() or not rei_dir.is_dir():
            raise HTTPException(status_code=500, detail="Default skin 'Rei' missing")

        # find VRM inside Rei
        rei_vrm = None
        for p in rei_dir.rglob('*.vrm'):
            rei_vrm = p
            break
        if not rei_vrm:
            raise HTTPException(status_code=500, detail="Default skin 'Rei' has no VRM to restore")

        temp_dir = Path(__file__).resolve().parent.parent / "res" / "synth_webui" / "skins" / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        target = temp_dir / 'model.vrm'
        import shutil
        try:
            shutil.copy2(rei_vrm, target)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to restore Rei VRM to temp: {exc}")
            raise HTTPException(status_code=500, detail="Failed to restore default VRM")

        try:
            self._set_active_vrm('model.vrm')
        except Exception:
            pass

        return JSONResponse({'status': 'ok', 'restored_from': str(rei_vrm)}, status_code=200)

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
        for p in target_skin.rglob('*.vrm'):
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
            log_error(f"{LOG_PREFIX} Failed to copy VRM when activating skin {skin_name}: {exc}")
            raise HTTPException(status_code=500, detail="Failed to activate skin")

        try:
            self._set_active_vrm(target.name)
        except Exception as exc:
            log_error(f"{LOG_PREFIX} Failed to set active VRM after activating skin {skin_name}: {exc}")
            raise HTTPException(status_code=500, detail="Failed to activate skin")

        return JSONResponse({'status': 'ok', 'name': target.name}, status_code=201)

    # ------------------------------------------------------------------
    # WebSocket logic
    # ------------------------------------------------------------------


async def start_server() -> None:
    """Compatibility helper to run the SyntH Web UI server in the foreground."""
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
    # Interface is already registered in __init__, no need to register again
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
