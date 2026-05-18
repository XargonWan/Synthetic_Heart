from __future__ import annotations

import asyncio
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
    "RADIO_HOST_VOX_ENGINE",
    label="Radio Host TTS Engine",
    default="",
    value_type=str,
    ui_type="string",
    description="Vox engine for on-air voice (leave empty to inherit default)",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "RADIO_HOST_POLL_INTERVAL_S",
    label="Track Poll Interval (s)",
    default=15,
    value_type=int,
    ui_type="number",
    description="How often to poll AzuraCast for track changes",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "RADIO_HOST_INTERMISSION",
    label="Songs Between Comments",
    default=1,
    value_type=int,
    ui_type="number",
    description="Number of songs to play before Synth speaks (1 = every song)",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "RADIO_HOST_LISTENER_HISTORY",
    label="Listener History Count",
    default=5,
    value_type=int,
    ui_type="number",
    description="How many recent listener messages to include for context",
    scope="plugins",
    component="radio_host",
)

INTERNAL_CHAT_ID = -2
BEAT_PENDING_FLAG = "_radio_beat_pending"


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
        self._config_listeners: list[str] = []

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
                value_type=int,
                group="plugins",
                component="radio_host",
            )
        )
        self._intermission = int(
            config_registry.get_value(
                "RADIO_HOST_INTERMISSION",
                1,
                value_type=int,
                group="plugins",
                component="radio_host",
            )
        )
        self._listener_history = int(
            config_registry.get_value(
                "RADIO_HOST_LISTENER_HISTORY",
                5,
                value_type=int,
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
            if self._monitor:
                self._monitor.update_config(
                    station_id=self._station_id,
                    poll_interval_s=self._poll_interval,
                    intermission=self._intermission,
                )
            if self._enabled and not self._running:
                asyncio.create_task(self._ensure_running())
            elif not self._enabled and self._running:
                asyncio.create_task(self._ensure_stopped())

        for key in (
            "RADIO_HOST_ENABLED",
            "AZURACAST_BASE_URL",
            "AZURACAST_API_KEY",
            "AZURACAST_STATION_ID",
            "RADIO_HOST_POLL_INTERVAL_S",
            "RADIO_HOST_INTERMISSION",
            "RADIO_HOST_LISTENER_HISTORY",
        ):
            config_registry.add_listener(key, _reload)
            self._config_listeners.append(key)

    async def start(self) -> None:
        await init_radio_tables()
        self._register_config_listeners()
        if self._enabled:
            await self._ensure_running()
        log_info("[radio_host] RadioHostPlugin initialized")

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
    ) -> None:
        if not self._running:
            return

        from core import message_queue

        beat_key = f"radio_{prev_title}_{curr_title}"
        if getattr(RadioHostPlugin, BEAT_PENDING_FLAG, None) == beat_key:
            log_debug(f"[radio_host] Beat already pending: {beat_key}")
            return

        setattr(RadioHostPlugin, BEAT_PENDING_FLAG, beat_key)

        context_text = (
            f"Song '{prev_title}' by {prev_artist} just finished.\n"
            f"Now playing: '{curr_title}' by {curr_artist}.\n"
        )
        if next_title and next_artist:
            context_text += f"Next up: '{next_title}' by {next_artist}.\n"
        context_text += (
            "Generate a short DJ transition (1-3 sentences). "
            "Be yourself — your personality, your mood, your sense of humor. "
            "Sometimes simple ('That was X by Y, and now...'), "
            "sometimes playful or clever."
        )

        message = SimpleNamespace()
        message.chat_id = INTERNAL_CHAT_ID
        message.message_id = f"radio_speak_{asyncio.get_event_loop().time()}"
        message.text = context_text
        message.from_user = SimpleNamespace(
            id=INTERNAL_CHAT_ID,
            username="radio_host",
            full_name="Radio Host",
            first_name="Radio Host",
        )
        message.chat = SimpleNamespace(id=INTERNAL_CHAT_ID, type="internal")
        message.date = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )

        context_memory = {
            "radio_host": True,
            "skip_history": False,
            "skip_current_chat": True,
            "history_recent_max": self._listener_history,
            "beat_type": "radio_transition",
            "allowed_action_types": ["radio_speak", "radio_update_metadata"],
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
                f"[radio_host] Enqueued transition: '{prev_title}' -> '{curr_title}'"
            )
        except Exception as e:
            log_error(f"[radio_host] Failed to enqueue: {e}")
        finally:
            asyncio.create_task(self._reset_pending_delay(beat_key))

    async def _reset_pending_delay(self, beat_key: str) -> None:
        await asyncio.sleep(30)
        current = getattr(RadioHostPlugin, BEAT_PENDING_FLAG, None)
        if current == beat_key:
            setattr(RadioHostPlugin, BEAT_PENDING_FLAG, None)

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

            injector_result = await self._injector.inject_banter(text, style)

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
                            (
                                context.get("current_track_title", ""),
                                context.get("current_track_artist", ""),
                                text,
                                style,
                                injector_result.get("status", "unknown"),
                            ),
                        )
                        await conn.commit()
            except Exception as e:
                log_debug(f"[radio_host] Failed to log activity: {e}")

            if injector_result.get("status") == "success":
                setattr(RadioHostPlugin, BEAT_PENDING_FLAG, None)
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


PLUGIN_CLASS = RadioHostPlugin
