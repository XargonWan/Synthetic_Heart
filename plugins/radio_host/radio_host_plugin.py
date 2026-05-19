from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.variables_engine import register_exposed_var

from .azuracast_client import AzuraCastClient
from .db import init_radio_tables
from .jingle_injector import JingleInjector
from .track_monitor import TrackMonitor

register_exposed_var(
    "RADIO_HOST_ENABLED",
    label="Radio Host Enabled",
    default=False,
    value_type=bool,
    ui_type="toggle",
    description="Enable Synth as an AI radio DJ",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "AZURACAST_BASE_URL",
    label="AzuraCast Base URL",
    default="",
    value_type=str,
    ui_type="string",
    description="AzuraCast instance URL (e.g. https://radio.example.com)",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "AZURACAST_API_KEY",
    label="AzuraCast API Key",
    default="",
    value_type=str,
    ui_type="password",
    description="AzuraCast API key with station management permissions",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "AZURACAST_STATION_ID",
    label="AzuraCast Station ID",
    default="",
    value_type=str,
    ui_type="string",
    description="Station shortcode from AzuraCast (e.g. 'main')",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "RADIO_HOST_LANGUAGE",
    label="Radio Language",
    default="English",
    value_type=str,
    ui_type="string",
    description="Language for radio DJ comments (e.g. English, Italian, Spanish)",
    scope="plugins",
    component="radio_host",
)

INTERNAL_CHAT_ID = -2


class RadioHostPlugin:
    display_name = "Radio Host"

    def __init__(self):
        register_plugin("radio_host", self)
        log_info("[radio_host] RadioHostPlugin registered")

        self._client = AzuraCastClient()
        self._injector = JingleInjector(self._client, "")
        self._monitor: TrackMonitor | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._pending_banter: dict[str, Any] | None = None
        self._read_config()

    def _read_config(self) -> None:
        self._enabled = bool(
            config_registry.get_value(
                "RADIO_HOST_ENABLED",
                False,
                value_type=bool,
                group="plugins",
                component="radio_host",
            )
        )
        base_url = str(
            config_registry.get_value(
                "AZURACAST_BASE_URL",
                "",
                value_type=str,
                group="plugins",
                component="radio_host",
            )
        )
        api_key = str(
            config_registry.get_value(
                "AZURACAST_API_KEY",
                "",
                value_type=str,
                group="plugins",
                component="radio_host",
            )
        )
        self._station_id = str(
            config_registry.get_value(
                "AZURACAST_STATION_ID",
                "",
                value_type=str,
                group="plugins",
                component="radio_host",
            )
        )
        self._poll_interval = int(
            config_registry.get_value(
                "RADIO_HOST_POLL_INTERVAL_S",
                15,
                label="Track Poll Interval (s)",
                description="How often to poll AzuraCast for track changes (default 15s)",
                value_type=int,
                group="plugins",
                component="radio_host",
            )
        )
        self._intermission = int(
            config_registry.get_value(
                "RADIO_HOST_INTERMISSION",
                1,
                label="Songs Between Comments",
                description="Number of songs to play before Synth speaks (1 = every song)",
                value_type=int,
                group="plugins",
                component="radio_host",
            )
        )
        self._listener_history = int(
            config_registry.get_value(
                "RADIO_HOST_LISTENER_HISTORY",
                5,
                label="Listener History Count",
                description="How many recent listener messages to include for prompt context",
                value_type=int,
                group="plugins",
                component="radio_host",
            )
        )
        self._language = str(
            config_registry.get_value(
                "RADIO_HOST_LANGUAGE",
                "English",
                value_type=str,
                group="plugins",
                component="radio_host",
            )
        )

        self._client.update_config(base_url, api_key)
        if self._injector:
            self._injector.update_station(self._station_id)

    def _register_config_listeners(self) -> None:
        def _reload(value: Any) -> None:
            self._read_config()
            if self._enabled and not self._running:
                asyncio.create_task(self._ensure_running())
            elif not self._enabled and self._running:
                asyncio.create_task(self._ensure_stopped())

        for key in (
            "RADIO_HOST_ENABLED",
            "AZURACAST_BASE_URL",
            "AZURACAST_API_KEY",
            "AZURACAST_STATION_ID",
            "RADIO_HOST_LANGUAGE",
        ):
            config_registry.add_listener(key, _reload)

    async def start(self) -> None:
        await init_radio_tables()
        self._register_config_listeners()
        await self._register_webui_routes()
        if self._enabled:
            await self._ensure_running()
        log_info("[radio_host] RadioHostPlugin initialized")

    async def _register_webui_routes(self) -> None:
        try:
            from core.webui import synth_webui_interface

            if synth_webui_interface is None:
                return
            app = synth_webui_interface.app
            js_path = Path(__file__).parent / "radio_host.js"

            @app.middleware("http")
            async def radio_webui_middleware(request: Any, call_next: Any) -> Any:
                path = request.url.path

                if path == "/templates/radio.html":
                    from starlette.responses import HTMLResponse

                    return HTMLResponse(self._render_tab_content())

                if path == "/js/plugins/radio_host.js":
                    from starlette.responses import Response

                    return Response(
                        js_path.read_text(),
                        media_type="application/javascript",
                    )

                return await call_next(request)

            @app.get("/api/radio/data")
            async def radio_api_data() -> dict:
                return await self._build_radio_data()

            log_info("[radio_host] WebUI routes registered")
        except Exception as e:
            log_debug(f"[radio_host] WebUI routes not available: {e}")

    def _render_tab_content(self) -> str:
        if not self._enabled or not self._client.configured:
            return self._render_disabled_form()
        return ""

    def _render_disabled_form(self) -> str:
        return """
<section class="tab-panel active" id="tab-radio" data-tab="radio" role="tabpanel">
  <link rel="stylesheet" href="/static/css/history.css">
  <div class="history-wrapper">
    <header class="section-header">
      <h2>📻 Radio Host</h2>
    </header>
    <div class="setup-card" style="max-width:600px;margin:40px auto;padding:32px;background:var(--surface-color);border-radius:12px;text-align:center;">
      <h3 style="margin:0 0 12px;font-size:1.3em;">Synth AI Radio DJ</h3>
      <p style="margin:0 0 8px;color:var(--text-muted);line-height:1.5;">
        Synth can be your AI radio DJ, automatically generating spoken
        transitions between songs on your AzuraCast station.
      </p>
      <hr style="margin:20px 0;border-color:var(--border-color);">
      <div style="text-align:left;">
        <strong>To get started:</strong>
        <div style="padding:8px 0;">
          <span style="display:inline-block;width:24px;height:24px;line-height:24px;text-align:center;border-radius:50%;background:var(--accent-color);color:#fff;font-size:0.8em;font-weight:700;margin-right:8px;">1</span>
          Go to the <strong>Config</strong> tab
        </div>
        <div style="padding:8px 0;">
          <span style="display:inline-block;width:24px;height:24px;line-height:24px;text-align:center;border-radius:50%;background:var(--accent-color);color:#fff;font-size:0.8em;font-weight:700;margin-right:8px;">2</span>
          Fill in: <strong>AzuraCast Base URL</strong>,
          <strong>API Key</strong>, and <strong>Station ID</strong>
        </div>
        <div style="padding:8px 0;">
          <span style="display:inline-block;width:24px;height:24px;line-height:24px;text-align:center;border-radius:50%;background:var(--accent-color);color:#fff;font-size:0.8em;font-weight:700;margin-right:8px;">3</span>
          Toggle <strong>Radio Host Enabled</strong> to ON
        </div>
      </div>
    </div>
  </div>
</section>
"""

    async def stop(self) -> None:
        await self._ensure_stopped()
        await self._client.close()
        await self._injector.cleanup()
        log_info("[radio_host] RadioHostPlugin stopped")

    async def _ensure_running(self) -> None:
        if self._running:
            return
        if not self._client.configured:
            log_warning("[radio_host] AzuraCast not configured; cannot start")
            return
        self._running = True
        self._monitor = TrackMonitor(
            client=self._client,
            station_id=self._station_id,
            poll_interval_s=self._poll_interval,
            intermission=self._intermission,
            on_track_change=self._on_track_change,
        )
        await self._monitor.start()
        log_info("[radio_host] Radio host started")

    async def _ensure_stopped(self) -> None:
        self._running = False
        if self._monitor:
            await self._monitor.stop()
            self._monitor = None
        log_info("[radio_host] Radio host stopped")

    async def _on_track_change(
        self,
        prev_title: str,
        prev_artist: str,
        curr_title: str,
        curr_artist: str,
        next_title: str | None = None,
        next_artist: str | None = None,
        should_comment: bool = True,
    ) -> None:
        if not self._running:
            return

        from core import message_queue

        # Step 1: Inject any stored pre-generated banter for this transition
        if should_comment and self._pending_banter:
            banter = self._pending_banter
            self._pending_banter = None

            expected_prev = (banter.get("prev_title"), banter.get("prev_artist"))
            if expected_prev == (prev_title, prev_artist):
                log_info(
                    f"[radio_host] Injecting pre-generated banter: "
                    f"'{prev_title}' -> '{curr_title}'"
                )
                result = await self._injector.inject_banter(
                    banter["text"], banter.get("style", "transition")
                )
                await self._log_activity(
                    prev_title,
                    prev_artist,
                    banter["text"],
                    banter.get("style", "transition"),
                    result.get("status", "unknown"),
                )
            else:
                log_debug(
                    "[radio_host] Stored banter didn't match transition; skipping"
                )

        # Step 2: Pre-generate banter for the next transition (curr -> next)
        if not next_title or not next_artist:
            return

        lang = self._language.strip() or "English"
        context_text = (
            f"Song '{curr_title}' by {curr_artist} just finished.\n"
            f"Now playing: '{next_title}' by {next_artist}.\n"
            f"Write your response in {lang}.\n"
            "Generate a short DJ transition (1-3 sentences). "
            "Be yourself — your personality, your mood, your sense of humor. "
            "Sometimes simple ('That was X by Y, and now...'), "
            "sometimes playful or clever."
        )

        message = SimpleNamespace()
        message.chat_id = INTERNAL_CHAT_ID
        message.message_id = f"radio_pregenerate_{asyncio.get_event_loop().time()}"
        message.text = context_text
        message.from_user = SimpleNamespace(
            id=INTERNAL_CHAT_ID,
            username="radio_host",
            full_name="Radio Host",
            first_name="Radio Host",
        )
        message.chat = SimpleNamespace(id=INTERNAL_CHAT_ID, type="internal")
        message.date = datetime.now(timezone.utc)

        context_memory: dict[str, Any] = {
            "radio_host": True,
            "radio_pre_generating": True,
            "radio_pregenerate_prev_title": curr_title,
            "radio_pregenerate_prev_artist": curr_artist,
            "radio_pregenerate_next_title": next_title,
            "radio_pregenerate_next_artist": next_artist,
            "skip_history": False,
            "skip_current_chat": True,
            "history_recent_max": self._listener_history,
            "beat_type": "radio_pregenerate",
            "allowed_action_types": ["radio_speak"],
        }

        try:
            await message_queue.enqueue_low_priority(
                None,
                message,
                context_memory=context_memory,
                interface_id="radio_host",
                original_message=None,
            )
            log_info(
                f"[radio_host] Pre-generating banter: '{curr_title}' -> '{next_title}'"
            )
        except Exception as e:
            log_error(f"[radio_host] Failed to enqueue pre-generate: {e}")

    def get_supported_actions(self) -> dict:
        return {
            "radio_speak": {
                "description": "Speak a DJ comment on the radio stream",
                "schema": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The spoken comment (1-3 sentences)",
                        },
                        "style": {
                            "type": "string",
                            "enum": [
                                "transition",
                                "intro",
                                "outro",
                                "news",
                                "shoutout",
                            ],
                            "description": "Context for the comment",
                        },
                    },
                    "required": ["text"],
                },
                "examples": {
                    "description": (
                        "Generate a DJ transition between songs. "
                        "Use your personality and mood from context."
                    ),
                },
            },
            "radio_update_metadata": {
                "description": "Update nowplaying metadata on AzuraCast",
                "schema": {
                    "type": "object",
                    "properties": {
                        "artist": {
                            "type": "string",
                            "description": "Artist name",
                        },
                        "title": {
                            "type": "string",
                            "description": "Song title",
                        },
                        "album": {
                            "type": "string",
                            "description": "Optional album name",
                        },
                    },
                    "required": ["artist", "title"],
                },
            },
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        if action_name == "radio_speak":
            return {
                "description": (
                    "Generate a short radio DJ transition comment (1-3 sentences). "
                    "Be natural and match your personality. "
                    "You can reference the current mood, memories, "
                    "or recent conversations from your context."
                ),
                "payload": {
                    "text": {
                        "type": "string",
                        "description": "The spoken transition text",
                    },
                    "style": {
                        "type": "string",
                        "description": "'transition' for between-song banter",
                        "optional": True,
                    },
                },
            }
        if action_name == "radio_update_metadata":
            return {
                "description": (
                    "Update the now-playing metadata on the radio stream "
                    "with the current song's artist and title."
                ),
                "payload": {
                    "artist": {"type": "string"},
                    "title": {"type": "string"},
                    "album": {"type": "string", "optional": True},
                },
            }
        return {}

    async def _log_activity(
        self,
        track_title: str,
        track_artist: str,
        banter_text: str,
        style: str,
        status: str,
    ) -> None:
        from .db import init_radio_tables

        await init_radio_tables()
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO radio_activity_log "
                        "(track_title, track_artist, banter_text, style, status) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (track_title, track_artist, banter_text, style, status),
                    )
                    await conn.commit()
        except Exception as e:
            log_debug(f"[radio_host] Failed to log activity: {e}")

    async def execute_action(
        self,
        action: dict,
        context: dict,
        bot: Any,
        original_message: Any,
    ) -> dict[str, Any]:
        action_type = action.get("type")
        payload = action.get("payload", {}) or {}

        if action_type == "radio_speak":
            text = payload.get("text", "")
            style = payload.get("style", "transition")

            if not text or not text.strip():
                return {"status": "skipped", "reason": "empty_text"}

            # Pre-generation mode: store for later injection
            if context.get("radio_pre_generating"):
                self._pending_banter = {
                    "text": text,
                    "style": style,
                    "prev_title": context.get("radio_pregenerate_prev_title", ""),
                    "prev_artist": context.get("radio_pregenerate_prev_artist", ""),
                    "curr_title": context.get("radio_pregenerate_next_title", ""),
                    "curr_artist": context.get("radio_pregenerate_next_artist", ""),
                }
                log_info(
                    f"[radio_host] Stored pre-generated banter for "
                    f"'{self._pending_banter['prev_title']}' -> "
                    f"'{self._pending_banter['curr_title']}'"
                )
                return {"status": "stored", "output": text}

            # Normal: TTS + upload + log
            injector_result = await self._injector.inject_banter(text, style)
            await self._log_activity(
                context.get("current_track_title", ""),
                context.get("current_track_artist", ""),
                text,
                style,
                injector_result.get("status", "unknown"),
            )

            if injector_result.get("status") == "success":
                return {"status": "success", "output": text}

            return injector_result

        if action_type == "radio_update_metadata":
            artist = payload.get("artist", "")
            title = payload.get("title", "")
            album = payload.get("album")

            if artist and title and self._client.configured:
                ok = await self._client.update_nowplaying_metadata(
                    self._station_id, artist, title, album
                )
                return {"status": "success" if ok else "error"}

            return {"status": "skipped", "reason": "missing_fields"}

        return {"status": "error", "message": f"Unknown action: {action_type}"}

    async def _build_radio_data(self) -> dict[str, Any]:
        activities: list[dict[str, Any]] = []
        try:
            from core.db import DictCursor, get_conn_ctx

            await init_radio_tables()
            async with get_conn_ctx() as conn:
                async with conn.cursor(DictCursor) as cur:
                    await cur.execute(
                        "SELECT timestamp, track_title, track_artist, "
                        "banter_text, style, status "
                        "FROM radio_activity_log "
                        "ORDER BY timestamp DESC LIMIT 50"
                    )
                    rows = await cur.fetchall()
                    if rows:
                        for r in rows:
                            activities.append(
                                {
                                    "timestamp": str(r.get("timestamp") or ""),
                                    "track_title": str(r.get("track_title") or ""),
                                    "track_artist": str(r.get("track_artist") or ""),
                                    "banter_text": str(r.get("banter_text") or ""),
                                    "style": str(r.get("style") or "transition"),
                                    "status": str(r.get("status") or ""),
                                }
                            )
        except Exception as e:
            log_debug(f"[radio_host] Failed to fetch activity log: {e}")

        return {
            "enabled": self._enabled,
            "configured": self._client.configured,
            "online": self._running and self._client.configured,
            "poll_interval": self._poll_interval,
            "intermission": self._intermission,
            "language": self._language,
            "station_id": self._station_id or "",
            "base_url": self._client._base_url if self._client.configured else "",
            "activities": activities,
        }


PLUGIN_CLASS = RadioHostPlugin
