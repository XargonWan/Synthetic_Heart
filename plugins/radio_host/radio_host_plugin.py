from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.variables_engine import register_exposed_var

from pathlib import Path as _Path

from .azuracast_client import AzuraCastClient
from .db import init_radio_tables, trim_old_audio
from .jingle_injector import JingleInjector
from .track_monitor import TrackMonitor

# Persistent directory for the last N banter audio files (for WebUI replay)
AUDIO_STORAGE_DIR = _Path("/app/tmp_tts/radio_host")
AUDIO_KEEP_COUNT = 30

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
        register_plugin("radio_host_plugin", self)
        log_info("[radio_host] RadioHostPlugin registered")

        self._client = AzuraCastClient()
        self._injector = JingleInjector(
            self._client, "", audio_storage_dir=str(AUDIO_STORAGE_DIR)
        )
        self._monitor: TrackMonitor | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._pending_banter: dict[str, Any] | None = None
        self._station_info_ts: float = 0.0
        self._STATION_INFO_TTL = 300
        self._station_name = ""
        self._schedule_desc = ""
        self._recent_activities: deque[dict[str, Any]] = deque(maxlen=50)
        self._webui_registered = False
        self._webui_registration_task: asyncio.Task | None = None
        self._webui_wait_logged = False
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
        self._station_info_ts = 0.0
        if not base_url or not api_key or not self._station_id:
            self._station_name = ""
            self._schedule_desc = ""

    def _register_config_listeners(self) -> None:
        def _reload(value: Any) -> None:
            self._read_config()
            if self._enabled and not self._running:
                asyncio.create_task(self._ensure_running())
            elif not self._enabled and self._running:
                asyncio.create_task(self._ensure_stopped())
            elif self._enabled and self._running:
                asyncio.create_task(self._update_station_info())

        for key in (
            "RADIO_HOST_ENABLED",
            "AZURACAST_BASE_URL",
            "AZURACAST_API_KEY",
            "AZURACAST_STATION_ID",
            "RADIO_HOST_LANGUAGE",
        ):
            config_registry.add_listener(key, _reload)

    async def start(self) -> None:
        self._register_config_listeners()
        await self._ensure_webui_routes_registered()
        try:
            await init_radio_tables()
        except Exception as e:
            log_warning(
                f"[radio_host] Radio activity DB unavailable; continuing with in-memory fallback: {e}"
            )
        if self._enabled:
            await self._ensure_running()
        else:
            log_info("[radio_host] Radio host disabled in config")
        log_info("[radio_host] RadioHostPlugin initialized")

    def _has_runtime_config(self) -> bool:
        return bool(self._station_id) and self._client.configured

    async def _ensure_webui_routes_registered(self) -> None:
        await self._register_webui_routes()
        if self._webui_registered or self._webui_registration_task is not None:
            return
        log_info("[radio_host] Scheduling deferred Activity integration task")
        self._webui_registration_task = asyncio.create_task(
            self._wait_for_webui_interface()
        )

    async def _wait_for_webui_interface(self) -> None:
        try:
            log_info("[radio_host] Deferred Activity integration task started")
            for attempt in range(30):
                if self._webui_registered:
                    return
                await asyncio.sleep(1)
                log_debug(f"[radio_host] Activity integration retry {attempt + 1}/30")
                await self._register_webui_routes()
                if self._webui_registered:
                    return
            log_warning(
                "[radio_host] WebUI not available; Radio Activity integration disabled"
            )
        finally:
            self._webui_registration_task = None

    async def _register_webui_routes(self) -> None:
        if self._webui_registered:
            return
        try:
            from core.webui import synth_webui_interface

            if synth_webui_interface is None:
                if not self._webui_wait_logged:
                    log_info(
                        "[radio_host] WebUI not ready yet; deferring Activity integration"
                    )
                    self._webui_wait_logged = True
                return

            js_path = Path(__file__).parent / "radio_host.js"
            synth_webui_interface.register_plugin_js(
                "radio_host", js_path.read_text(encoding="utf-8")
            )
            synth_webui_interface.register_plugin_api_route(
                "/api/radio/data", self._build_radio_data
            )
            synth_webui_interface.register_plugin_api_route(
                "/api/radio/audio", self._serve_radio_audio
            )
            synth_webui_interface.register_plugin_section_tab(
                "history",
                "radio_host",
                (
                    '<button class="sub-nav-btn" id="history-radio-plugin-btn"'
                    ' type="button" data-subtab="radio" role="tab"'
                    ' aria-selected="false">'
                    '<span class="icon">📻</span><span>Radio</span></button>'
                ),
                (
                    '<div class="sub-tab-panel" id="subtab-radio"'
                    ' data-subtab="radio">'
                    '<div class="loading-state"><div class="loading-spinner"></div>'
                    "<p>Loading radio activity...</p></div></div>"
                ),
            )

            self._webui_registered = True
            self._webui_wait_logged = False
            log_info("[radio_host] Radio Activity integration registered")
        except Exception as e:
            log_warning(f"[radio_host] Activity integration setup failed: {e}")

    async def stop(self) -> None:
        if self._webui_registration_task and not self._webui_registration_task.done():
            self._webui_registration_task.cancel()
            try:
                await self._webui_registration_task
            except asyncio.CancelledError:
                pass
            self._webui_registration_task = None
        await self._ensure_stopped()
        await self._client.close()
        await self._injector.cleanup()
        log_info("[radio_host] RadioHostPlugin stopped")

    async def _ensure_running(self) -> None:
        if self._running:
            return
        if not self._client.configured:
            log_warning(
                "[radio_host] AzuraCast base URL or API key missing; cannot start"
            )
            return
        if not self._station_id:
            log_warning("[radio_host] AzuraCast station ID missing; cannot start")
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
        await self._update_station_info()
        log_info("[radio_host] Radio host started")

    async def _ensure_stopped(self) -> None:
        self._running = False
        if self._monitor:
            await self._monitor.stop()
            self._monitor = None
        log_info("[radio_host] Radio host stopped")

    async def _update_station_info(self) -> None:
        now = asyncio.get_event_loop().time()
        if now - self._station_info_ts < self._STATION_INFO_TTL:
            return
        self._station_info_ts = now
        self._station_name = ""
        self._schedule_desc = ""

        if not self._client.configured:
            return

        try:
            api_name = await self._client.get_station_name(self._station_id)
            if api_name:
                self._station_name = api_name
        except Exception as e:
            log_warning(f"[radio_host] Station name fetch failed: {e}")

        try:
            schedules = await self._client.get_station_schedule(self._station_id)
            active = self._find_active_schedule(schedules)
            if active:
                desc = active.get("description", "") or active.get("name", "")
                if desc:
                    self._schedule_desc = desc
        except Exception as e:
            log_warning(f"[radio_host] Schedule fetch failed: {e}")

    def _find_active_schedule(self, schedules: list[dict]) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        now_seconds = now.hour * 3600 + now.minute * 60 + now.second
        day = now.isoweekday()

        for s in schedules:
            if not s.get("is_enabled", True):
                continue
            s_days = s.get("days", [])
            if day not in s_days:
                continue
            start = s.get("start_time", 0)
            end = s.get("end_time", 86400)
            if start <= now_seconds < end:
                return s

        return None

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

        await self._update_station_info()
        injected_current_transition = False

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
                    audio_path=result.get("audio_path"),
                )
                injected_current_transition = True
            else:
                log_debug(
                    "[radio_host] Stored banter didn't match transition; skipping"
                )

        # Step 2: If AzuraCast does not expose a next song, fall back to
        # generating the transition live for the current track change.
        if not next_title or not next_artist:
            if should_comment and not injected_current_transition:
                log_info(
                    f"[radio_host] No playing_next data; generating live banter for '{prev_title}' -> '{curr_title}'"
                )
                await self._enqueue_banter_generation(
                    prev_title=prev_title,
                    prev_artist=prev_artist,
                    curr_title=curr_title,
                    curr_artist=curr_artist,
                    pre_generate=False,
                )
            elif should_comment:
                log_debug(
                    f"[radio_host] No playing_next data; using already-injected banter for '{prev_title}' -> '{curr_title}'"
                )
            else:
                log_debug(
                    f"[radio_host] No playing_next data; skipping comment for '{prev_title}' -> '{curr_title}' due to intermission"
                )
            return

        # Step 3: Pre-generate banter for the next transition (curr -> next)
        await self._enqueue_banter_generation(
            prev_title=curr_title,
            prev_artist=curr_artist,
            curr_title=next_title,
            curr_artist=next_artist,
            pre_generate=True,
        )

    async def _enqueue_banter_generation(
        self,
        prev_title: str,
        prev_artist: str,
        curr_title: str,
        curr_artist: str,
        pre_generate: bool,
    ) -> None:
        from core import message_queue

        lang = self._language.strip() or "English"
        desc = self._schedule_desc.strip()
        station = self._station_name.strip()
        context_text_parts = [
            f"Song '{prev_title}' by {prev_artist} just finished.",
            f"Now playing: '{curr_title}' by {curr_artist}.",
        ]
        if station:
            context_text_parts.append(f"You are on {station}.")
        if desc:
            context_text_parts.append(f"Current program: {desc}.")
        context_text_parts.extend(
            [
                f"Write your response in {lang}.",
                "Generate a short DJ transition (1-3 sentences). "
                "Be yourself — your personality, your mood, your sense of humor. "
                "Sometimes simple ('That was X by Y, and now...'), "
                "sometimes playful or clever.",
                "IMPORTANT: Respond using ONLY the 'radio_speak' action type. "
                "Do NOT use 'message_send', 'send_message', 'message', or any other type.",
            ]
        )
        context_text = "\n".join(context_text_parts)

        message = SimpleNamespace()
        message.chat_id = INTERNAL_CHAT_ID
        prefix = "radio_pregenerate" if pre_generate else "radio_live"
        message.message_id = f"{prefix}_{asyncio.get_event_loop().time()}"
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
            "current_track_title": curr_title,
            "current_track_artist": curr_artist,
            "skip_history": True,
            "history_recent_max": self._listener_history,
            "beat_type": "radio_pregenerate" if pre_generate else "radio_transition",
            "allowed_action_types": ["radio_speak"],
        }
        if pre_generate:
            context_memory.update(
                {
                    "radio_pre_generating": True,
                    "radio_pregenerate_prev_title": prev_title,
                    "radio_pregenerate_prev_artist": prev_artist,
                    "radio_pregenerate_next_title": curr_title,
                    "radio_pregenerate_next_artist": curr_artist,
                }
            )

        try:
            await message_queue.enqueue_low_priority(
                None,
                message,
                context_memory=context_memory,
                interface_id="radio_host",
                original_message=None,
            )
            if pre_generate:
                log_info(
                    f"[radio_host] Pre-generating banter: '{prev_title}' -> '{curr_title}'"
                )
            else:
                log_info(
                    f"[radio_host] Enqueued live banter: '{prev_title}' -> '{curr_title}'"
                )
        except Exception as e:
            phase = "pre-generate" if pre_generate else "live"
            log_error(f"[radio_host] Failed to enqueue {phase} banter: {e}")

    def _store_fallback_activity(
        self,
        track_title: str,
        track_artist: str,
        banter_text: str,
        style: str,
        status: str,
        audio_path: str | None = None,
    ) -> None:
        self._recent_activities.appendleft(
            {
                "id": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "track_title": track_title,
                "track_artist": track_artist,
                "banter_text": banter_text,
                "style": style or "transition",
                "status": status,
                "audio_url": None,
            }
        )

    def _fallback_activity_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._recent_activities]

    def get_supported_actions(self) -> dict:
        return {
            "radio_speak": {
                "brief": (
                    "Output your spoken DJ commentary on the radio stream. "
                    "Use THIS action (not message_send or send_message) "
                    "when you want to say something on air."
                ),
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
        audio_path: str | None = None,
    ) -> None:
        try:
            await init_radio_tables()
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO radio_activity_log "
                        "(track_title, track_artist, banter_text, style, status, banter_audio_file) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            track_title,
                            track_artist,
                            banter_text,
                            style,
                            status,
                            audio_path or None,
                        ),
                    )
                    await conn.commit()
            # Prune old audio files — keep only the last AUDIO_KEEP_COUNT
            asyncio.create_task(self._trim_audio_files())
        except Exception as e:
            self._store_fallback_activity(
                track_title,
                track_artist,
                banter_text,
                style,
                status,
                audio_path=audio_path,
            )
            log_warning(
                f"[radio_host] Failed to log activity to DB; using in-memory fallback: {e}"
            )

    async def _trim_audio_files(self) -> None:
        """Delete audio files for entries beyond the most recent AUDIO_KEEP_COUNT."""
        try:
            paths_to_delete = await trim_old_audio(keep=AUDIO_KEEP_COUNT)
            for path in paths_to_delete:
                try:
                    import os

                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
        except Exception as e:
            log_warning(f"[radio_host] Audio trim failed: {e}")

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
            text = (
                payload.get("text")
                or payload.get("body")
                or payload.get("content")
                or ""
            )
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
                audio_path=injector_result.get("audio_path"),
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

    async def _serve_radio_audio(self, request: Any) -> Any:
        """Serve a banter audio file by DB row id (query param ``id``)."""
        from starlette.requests import Request
        from starlette.responses import FileResponse, JSONResponse

        req: Request = request
        row_id = req.query_params.get("id", "")
        if not row_id or not row_id.isdigit():
            return JSONResponse({"error": "missing id"}, status_code=400)
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT banter_audio_file FROM radio_activity_log WHERE id = %s",
                        (int(row_id),),
                    )
                    row = await cur.fetchone()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        audio_path = (
            row[0] if isinstance(row, (tuple, list)) else row.get("banter_audio_file")
        )
        if not audio_path:
            return JSONResponse({"error": "no audio"}, status_code=404)

        import os

        if not os.path.isfile(audio_path):
            return JSONResponse({"error": "file missing"}, status_code=404)
        return FileResponse(audio_path, media_type="audio/wav")

    async def _build_radio_data(self) -> dict[str, Any]:
        activities: list[dict[str, Any]] = []
        try:
            from core.db import DictCursor, get_conn_ctx

            await init_radio_tables()
            async with get_conn_ctx() as conn:
                async with conn.cursor(DictCursor) as cur:
                    await cur.execute(
                        "SELECT id, timestamp, track_title, track_artist, "
                        "banter_text, style, status, banter_audio_file "
                        "FROM radio_activity_log "
                        "ORDER BY timestamp DESC LIMIT 50"
                    )
                    rows = await cur.fetchall()
                    if rows:
                        for r in rows:
                            row_id = r.get("id")
                            has_audio = bool(r.get("banter_audio_file"))
                            activities.append(
                                {
                                    "id": row_id,
                                    "timestamp": str(r.get("timestamp") or ""),
                                    "track_title": str(r.get("track_title") or ""),
                                    "track_artist": str(r.get("track_artist") or ""),
                                    "banter_text": str(r.get("banter_text") or ""),
                                    "style": str(r.get("style") or "transition"),
                                    "status": str(r.get("status") or ""),
                                    "audio_url": f"/api/radio/audio?id={row_id}"
                                    if has_audio
                                    else None,
                                }
                            )
        except Exception as e:
            log_warning(
                f"[radio_host] Failed to fetch activity log from DB; using in-memory fallback: {e}"
            )

        if not activities and self._recent_activities:
            activities = self._fallback_activity_rows()

        return {
            "enabled": self._enabled,
            "configured": self._has_runtime_config(),
            "online": self._running and self._has_runtime_config(),
            "poll_interval": self._poll_interval,
            "intermission": self._intermission,
            "language": self._language,
            "station_name": self._station_name or "",
            "schedule_description": self._schedule_desc or "",
            "station_id": self._station_id or "",
            "base_url": self._client._base_url if self._client.configured else "",
            "activities": activities,
        }


PLUGIN_CLASS = RadioHostPlugin
