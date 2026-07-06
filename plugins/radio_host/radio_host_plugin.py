from __future__ import annotations

import asyncio
import time as _time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.persona_manager import get_persona_manager
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
    "RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS",
    label="Announce Only With Listeners",
    default=True,
    value_type=bool,
    ui_type="toggle",
    description="When enabled, Synth only speaks on air if there is at least one listener. When disabled, announcements always play regardless of listener count.",
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

register_exposed_var(
    "RADIO_HOST_POLL_INTERVAL_S",
    label="Track Poll Interval (s)",
    default=15,
    value_type=int,
    ui_type="string",
    description="How often to poll AzuraCast for track changes (default 15s)",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "RADIO_HOST_INTERMISSION",
    label="Songs Between Comments",
    default=1,
    value_type=int,
    ui_type="string",
    description="Number of songs to play before Synth speaks (1 = every song)",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "RADIO_HOST_LISTENER_HISTORY",
    label="Listener History Count",
    default=5,
    value_type=int,
    ui_type="string",
    description="How many recent listener messages to include for prompt context",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "RADIO_HOST_VOX_ENGINE",
    label="Radio TTS Engine Override",
    default="",
    value_type=str,
    ui_type="string",
    description="Override the default TTS engine for radio host (leave blank to use default)",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "AZURACAST_STREAMER_USERNAME",
    label="WebDJ Streamer Username",
    default="SyntH",
    value_type=str,
    ui_type="string",
    description="AzuraCast streamer (WebDJ) account username used to broadcast banter",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "AZURACAST_STREAMER_PASSWORD",
    label="WebDJ Streamer Password",
    default="synthradio",
    value_type=str,
    ui_type="password",
    description="AzuraCast streamer (WebDJ) account password",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "RADIO_HOST_GAIN_DB",
    label="Audio Gain (dB)",
    default=4.0,
    value_type=float,
    ui_type="string",
    description="Volume boost for banter audio in dB (e.g. 4.0 for +4dB)",
    scope="plugins",
    component="radio_host",
)

register_exposed_var(
    "RADIO_HOST_NEXT_SONG_ANNOUNCEMENT",
    label="Next Song Announcement (EXPERIMENTAL)",
    default=False,
    value_type=bool,
    ui_type="toggle",
    description="(EXPERIMENTAL) When enabled, Synth announces the next song ('Avete ascoltato X, ora Y'). When disabled, Synth only de-announces the song that just finished ('Avete ascoltato X') without mentioning what's coming next.",
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
            self._client,
            "",
            audio_storage_dir=str(AUDIO_STORAGE_DIR),
        )
        self._monitor: TrackMonitor | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        # Pre-generated banter keyed by (prev_title, prev_artist). Multiple
        # transitions are pre-generated ahead of time (template + LLM per
        # transition), so a single slot would be overwritten by whichever
        # writer finishes last and almost never match the next transition.
        self._pending_banter: dict[tuple[str, str], dict[str, Any]] = {}
        self._inject_at_track_change = False
        # Tracks the ID of the last track for which _inject_at_track_change
        # was set to True.  Used to distinguish deferred injections that come
        # from a bumper/jingle (where actual injection should happen when the
        # next real song starts) from stale flags inherited from a previous
        # winding-down that already completed.
        self._inject_at_track_change_for_id: str | None = None
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
        self._gain_db = float(
            config_registry.get_value(
                "RADIO_HOST_GAIN_DB",
                4.0,
                value_type=float,
                group="plugins",
                component="radio_host",
            )
        )
        self._next_song_announcement = bool(
            config_registry.get_value(
                "RADIO_HOST_NEXT_SONG_ANNOUNCEMENT",
                False,
                value_type=bool,
                group="plugins",
                component="radio_host",
            )
        )
        self._announce_if_no_listeners = bool(
            config_registry.get_value(
                "RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS",
                True,
                value_type=bool,
                group="plugins",
                component="radio_host",
            )
        )
        self._streamer_username = (
            str(
                config_registry.get_value(
                    "AZURACAST_STREAMER_USERNAME",
                    "SyntH",
                    value_type=str,
                    group="plugins",
                    component="radio_host",
                )
            )
            or "SyntH"
        )
        self._streamer_password = (
            str(
                config_registry.get_value(
                    "AZURACAST_STREAMER_PASSWORD",
                    "synthradio",
                    value_type=str,
                    group="plugins",
                    component="radio_host",
                )
            )
            or "synthradio"
        )
        self._client.update_config(base_url, api_key)
        if self._injector:
            self._injector.update_station_shortcode(self._station_id)
            self._injector.update_gain(self._gain_db)
            self._injector.update_streamer_credentials(
                username=self._streamer_username,
                password=self._streamer_password,
            )
        # Update monitor config if it exists
        if self._monitor:
            self._monitor.update_config(
                station_id=self._station_id,
                poll_interval_s=self._poll_interval,
                intermission=self._intermission,
            )
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
            "RADIO_HOST_POLL_INTERVAL_S",
            "RADIO_HOST_INTERMISSION",
            "RADIO_HOST_LISTENER_HISTORY",
            "RADIO_HOST_VOX_ENGINE",
            "RADIO_HOST_GAIN_DB",
            "RADIO_HOST_NEXT_SONG_ANNOUNCEMENT",
            "RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS",
            "AZURACAST_STREAMER_USERNAME",
            "AZURACAST_STREAMER_PASSWORD",
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

    def is_enabled(self) -> bool:
        """Return True when the radio host plugin is enabled in config."""
        return self._enabled

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

    def _get_synth_name(self) -> str:
        """Return the configured synth name (e.g. 'SyntH')."""
        return str(config_registry.get_value("SYNTH_NAME", "SyntH") or "SyntH")

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
            on_winding_down=self._on_winding_down,
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
        now_ts = now.timestamp()
        now_seconds = now.hour * 3600 + now.minute * 60 + now.second
        day = now.isoweekday()

        for s in schedules:
            if not s.get("is_enabled", True):
                continue
            # AzuraCast's /api/station/{id}/schedule returns calendar entries
            # with an explicit is_now flag and unix start/end timestamps.
            if s.get("is_now"):
                return s
            start_ts = s.get("start_timestamp")
            end_ts = s.get("end_timestamp")
            if start_ts and end_ts:
                try:
                    if float(start_ts) <= now_ts < float(end_ts):
                        return s
                except (TypeError, ValueError):
                    pass
                continue
            # Legacy shape: days list plus seconds-in-day boundaries.
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
        queue_ahead: list[dict[str, str]] | None = None,
        prev_is_jingle: bool = False,
        curr_is_jingle: bool = False,
        winding_down_already_injected: bool = False,
        winding_down_deferred_for_prev: bool = False,
    ) -> None:
        if not self._running:
            return

        await self._update_station_info()

        log_info(
            f"[radio_host] Track change recorded: '{prev_title}' -> '{curr_title}'"
        )

        # Detect first listener arrival: reset intermission counter so the
        # announcement series starts fresh as soon as someone tunes in.
        if (
            self._announce_if_no_listeners
            and self._monitor
            and self._monitor.listener_data_available
            and self._monitor.last_listeners == 0
            and self._monitor.current_listeners > 0
        ):
            log_info(
                "[radio_host] First listener detected, resetting intermission counter"
            )
            self._track_count_since_comment = 0

        # Suppress announcements when there are no listeners and the feature
        # is enabled.  When listener data is not yet available, fall back to
        # the normal path so the plugin still works with older AzuraCast
        # instances that do not expose listener counts.
        if (
            self._announce_if_no_listeners
            and self._monitor
            and self._monitor.listener_data_available
            and self._monitor.current_listeners <= 0
        ):
            log_info("[radio_host] No listeners detected, skipping announcement")
            return

        # If next-song announcement is disabled, do NOT inject here.
        # The de-announce was already scheduled by _on_winding_down (during
        # the song's final seconds) and will broadcast in the clean gap
        # between tracks (after any bumper/jingle finishes).
        if not self._next_song_announcement:
            self._inject_at_track_change = False
            # Still pre-generate for upcoming transitions if queue data available
            self._pre_generate_from_queue(
                curr_title, curr_artist, next_title, next_artist, queue_ahead
            )
            return

        # Skip injection if winding-down already completed for the previous
        # track.  This prevents double announcements when a bumper/jingle
        # plays between two real songs: the winding-down already injected
        # banter for the transition, so _on_track_change must not inject again.
        if winding_down_already_injected:
            log_info(
                "[radio_host] Winding-down already injected for previous track; "
                "skipping track-change injection"
            )
            self._inject_at_track_change = False
            # Still pre-generate for upcoming transitions
            self._pre_generate_from_queue(
                curr_title, curr_artist, next_title, next_artist, queue_ahead
            )
            return

        # Deferred injection — winding-down was skipped because the previous
        # track was a bumper/jingle.  Inject the banter now that a real song
        # has started.
        if winding_down_deferred_for_prev and should_comment and not curr_is_jingle:
            log_info(
                "[radio_host] Injecting deferred banter (winding-down was skipped "
                "for bumper/jingle)"
            )
            self._inject_at_track_change = False
            asyncio.create_task(
                self._inject_banter_now(
                    prev_title, prev_artist, curr_title, curr_artist
                )
            )
            # Still pre-generate for upcoming transitions
            self._pre_generate_from_queue(
                curr_title, curr_artist, next_title, next_artist, queue_ahead
            )
            return

        # Fallback injection — only fires when winding_down was skipped (e.g.
        # the previous track was a jingle/bumper so no outro announcement was
        # made).  The main injection path is _on_winding_down (during the
        # song's outro) so the banter plays while the song is still on air.
        if self._inject_at_track_change and should_comment and not curr_is_jingle:
            self._inject_at_track_change = False
            asyncio.create_task(
                self._inject_banter_now(
                    prev_title, prev_artist, curr_title, curr_artist
                )
            )

        # Pre-generate for upcoming transitions using queue data.
        self._pre_generate_from_queue(
            curr_title, curr_artist, next_title, next_artist, queue_ahead
        )

    def _pre_generate_from_queue(
        self,
        curr_title: str,
        curr_artist: str,
        next_title: str | None,
        next_artist: str | None,
        queue_ahead: list[dict[str, str]] | None,
    ) -> None:
        """Pre-generate banter for upcoming transitions from queue data."""
        if queue_ahead and len(queue_ahead) >= 2:
            transitions_to_pregen = min(len(queue_ahead), 3)
            for i in range(transitions_to_pregen):
                if i == 0:
                    from_title = curr_title
                    from_artist = curr_artist
                else:
                    from_t = queue_ahead[i - 1]
                    from_title = from_t.get("title", "")
                    from_artist = from_t.get("artist", "")
                to_t = queue_ahead[i]
                if from_title and to_t.get("title"):
                    self._enqueue_pre_gen_banter(
                        from_title,
                        from_artist,
                        to_t["title"],
                        to_t["artist"],
                    )
            log_info(
                f"[radio_host] Queued {transitions_to_pregen} pre-generations from "
                f"queue data"
            )
        elif next_title and next_artist:
            log_info(
                f"[radio_host] Pre-generating LLM banter: "
                f"'{curr_title}' -> '{next_title}'"
            )
            self._enqueue_pre_gen_banter(
                curr_title, curr_artist, next_title, next_artist
            )
        else:
            # Clear flag if we can't pre-gen either (nothing upcoming known)
            self._inject_at_track_change = False

    async def _inject_banter_now(
        self,
        prev_title: str,
        prev_artist: str,
        curr_title: str,
        curr_artist: str,
        deannounce_only: bool = False,
    ) -> None:
        """Generate and broadcast banter for the *prev_title* → *curr_title* transition.

        When *deannounce_only* is True, only the previous track is mentioned
        (no reference to the next/current track).  This is used when
        ``RADIO_HOST_NEXT_SONG_ANNOUNCEMENT`` is disabled.

        Tries pre-generated audio first (stored at the previous track_change for
        this exact transition).  Falls back to on-the-fly LLM generation when no
        pre-gen is available or the queue changed in between.
        """
        banter_to_inject = self._pop_matching_banter(prev_title, prev_artist)
        if banter_to_inject:
            pre_gen_curr = banter_to_inject.get("curr_title", "")
            pre_gen_curr_artist = banter_to_inject.get("curr_artist", "")
            match = pre_gen_curr == curr_title and pre_gen_curr_artist == curr_artist
            if match:
                log_info(
                    f"[radio_host] Track change with pre-generated banter: "
                    f"'{prev_title}' -> '{curr_title}'"
                )
            else:
                log_info(
                    f"[radio_host] Pre-generated banter stale "
                    f"(was '{pre_gen_curr}', now '{curr_title}'); "
                    f"generating on the fly"
                )
                banter_to_inject = None

        if banter_to_inject is None:
            log_info(
                f"[radio_host] No pre-generated banter available; "
                f"generating LLM banter on the fly: '{prev_title}' -> '{curr_title}'"
            )
            banter_to_inject = await self._generate_banter_on_the_fly(
                prev_title=prev_title,
                prev_artist=prev_artist,
                curr_title=curr_title,
                curr_artist=curr_artist,
                deannounce_only=deannounce_only,
            )
            if banter_to_inject is None:
                log_error(
                    "[radio_host] On-the-fly LLM banter generation failed; "
                    "skipping injection"
                )
                return

        self._set_animation("speak")
        result = await self._injector.inject_banter(
            banter_to_inject["text"],
            banter_to_inject.get("style", "transition"),
            pre_generated_audio_path=banter_to_inject.get("audio_path"),
            synth_name=self._get_synth_name(),
        )
        # Mirror the same voice to the WebUI avatar so any connected spectator
        # sees and hears the synth speaking on air (lip-sync + expressions).
        await self._broadcast_to_webui(
            result.get("audio_path") or banter_to_inject.get("audio_path"),
            banter_to_inject["text"],
        )
        self._set_animation("idle")
        await self._log_activity(
            curr_title,
            curr_artist,
            banter_to_inject["text"],
            banter_to_inject.get("style", "transition"),
            result.get("status", "unknown"),
            audio_path=result.get("audio_path"),
        )

    async def _on_winding_down(
        self,
        curr_title: str,
        curr_artist: str,
        next_title: str | None = None,
        next_artist: str | None = None,
        remaining: float = 0,
    ) -> None:
        """Called when the current track is near its end (~45 s remaining).

        Primary injection point — generates banter, then schedules the
        broadcast to play EXACTLY when the song ends, so the announcement
        lands in the clean gap between songs (no overlap with AutoDJ
        jingles or bumpers that would play during the transition).

        When ``RADIO_HOST_NEXT_SONG_ANNOUNCEMENT`` is disabled, generates
        a de-announce (only mentions the song that just finished) but still
        uses the same timed injection pipeline so the broadcast lands in
        the clean gap, never over the start of the next song.

        Falls back to ``_inject_at_track_change`` when the current track is
        short / a jingle (no room to inject during the outro).

        Injection is launched as a background task so the track monitor's
        poll loop is not blocked.
        """
        if not self._running:
            return

        # Suppress announcements when there are no listeners and the feature
        # is enabled.  When listener data is not yet available, fall back to
        # the normal path so the plugin still works with older AzuraCast
        # instances that do not expose listener counts.
        if (
            self._announce_if_no_listeners
            and self._monitor
            and self._monitor.listener_data_available
            and self._monitor.current_listeners <= 0
        ):
            log_info(
                "[radio_host] No listeners detected, skipping winding-down announcement"
            )
            return

        await self._update_station_info()

        # Determine whether we have enough time / content to inject at
        # winding-down.  If the current track is a jingle/short, skip
        # and set the flag so the next track_change injects instead.
        playlist = self._monitor.current_playlist if self._monitor else ""
        is_jingle_playlist = "jingle" in playlist.lower()
        is_bumper_playlist = "bumper" in playlist.lower()
        duration = 0.0
        try:
            np = await self._client.get_nowplaying(self._station_id)
            current = np.get("now_playing", {}) or {}
            duration = current.get("duration", 0) or 0
        except Exception:
            pass
        is_short = duration < 45 and duration > 0

        if is_short or is_jingle_playlist or is_bumper_playlist:
            self._inject_at_track_change = True
            log_info(
                f"[radio_host] Winding down (short/jingle, deferring to track_change): "
                f"'{curr_title}' ({duration:.0f}s, playlist='{playlist}')"
            )
            return

        # Generate banter text - always use LLM, never templates
        if self._next_song_announcement:
            # Full transition announcement: mentions both previous and next song.
            fresh_next = await self._fetch_next_track_info()
            actual_next = (
                fresh_next.get("title")
                or next_title
                or (self._monitor.next_track_title if self._monitor else "")
                or ""
            )
            actual_next_artist = (
                fresh_next.get("artist")
                or next_artist
                or (self._monitor.next_track_artist if self._monitor else "")
                or ""
            )

            banter_to_inject = self._pop_matching_banter(curr_title, curr_artist)
            if banter_to_inject:
                pre_gen_next = banter_to_inject.get("curr_title", "")
                pre_gen_next_artist = banter_to_inject.get("curr_artist", "")
                match = (
                    pre_gen_next == actual_next
                    and pre_gen_next_artist == actual_next_artist
                )
                if match:
                    log_info(
                        f"[radio_host] Winding down with pre-generated banter: "
                        f"'{curr_title}' -> '{actual_next}'"
                    )
                else:
                    log_info(
                        f"[radio_host] Pre-generated banter stale "
                        f"(was '{pre_gen_next}', now '{actual_next}'); "
                        f"generating on the fly"
                    )
                    banter_to_inject = None

            if banter_to_inject is None:
                log_info(
                    f"[radio_host] No pre-generated banter available; "
                    f"generating LLM banter on the fly: '{curr_title}' -> '{actual_next}'"
                )
                banter_to_inject = await self._generate_banter_on_the_fly(
                    prev_title=curr_title,
                    prev_artist=curr_artist,
                    curr_title=actual_next,
                    curr_artist=actual_next_artist,
                    deannounce_only=False,
                )
                if banter_to_inject is None:
                    log_error(
                        "[radio_host] On-the-fly LLM banter generation failed; "
                        "skipping winding-down injection"
                    )
                    return
        else:
            # De-announce only: mention the song that just finished,
            # without mentioning what's coming next.  Still uses the
            # timed injection pipeline so it lands in the gap.
            log_info(
                f"[radio_host] No pre-generated banter available; "
                f"generating LLM banter on the fly (de-announce): '{curr_title}'"
            )
            banter_to_inject = await self._generate_banter_on_the_fly(
                prev_title=curr_title,
                prev_artist=curr_artist,
                curr_title="",
                curr_artist="",
                deannounce_only=True,
            )
            if banter_to_inject is None:
                log_error(
                    "[radio_host] On-the-fly LLM banter generation failed; "
                    "skipping winding-down injection"
                )
                return

        # Clear the fallback flag — we just injected
        self._inject_at_track_change = False

        # Compute the absolute timestamp when the song will end
        song_end_ts = _time.time() + remaining

        # Fire the injection pipeline as a background task.  The pipeline
        # generates TTS, then waits for the song to end + any bumper/jingle
        # in the transition gap, then broadcasts in the clean gap.
        asyncio.create_task(
            self._inject_winding_down_banter(
                banter_to_inject=banter_to_inject,
                curr_title=curr_title,
                curr_artist=curr_artist,
                song_end_ts=song_end_ts,
            )
        )

    async def _fetch_next_track_info(self) -> dict[str, str]:
        """Fetch fresh nowplaying data and extract the next-track info."""
        try:
            np = await self._client.get_nowplaying(self._station_id)
            playing_next = np.get("playing_next", {}) or {}
            song = playing_next.get("song", {}) or {}
            return {
                "title": str(song.get("title", "")),
                "artist": str(song.get("artist", "")),
            }
        except Exception:
            return {"title": "", "artist": ""}

    async def _wait_for_bumper_end(self, song_end_ts: float) -> None:
        """After *song_end_ts*, poll AzuraCast and wait until any
        bumper/jingle that started playing at song end finishes.
        Returns when a real song is playing (or a reasonable timeout).
        """
        # First, wait until song_end_ts (the winding-down song ends)
        now = _time.time()
        if song_end_ts > now:
            await asyncio.sleep(song_end_ts - now)

        # Now poll to detect if something short (bumper/jingle) started.
        # Wait up to 30 seconds for it to finish.
        max_wait = 30.0
        polled = 0.0
        while polled < max_wait:
            try:
                np = await self._client.get_nowplaying(self._station_id)
                current = np.get("now_playing", {}) or {}
                track = current.get("song", {}) or {}
                title = track.get("title", "") or ""
                if "is speaking" in title.lower():
                    return  # Our own banter is playing — proceed
                playlist = current.get("playlist", "") or ""
                elapsed = float(current.get("elapsed", 0) or 0)
                duration = float(current.get("duration", 0) or 0)
                remaining = duration - elapsed
                playlist_lower = playlist.lower()
                is_short = duration < 45 and duration > 0
                is_bumper = "bumper" in playlist_lower
                is_jingle_playlist = "jingle" in playlist_lower
                if is_bumper or is_jingle_playlist or is_short:
                    log_info(
                        f"[radio_host] Bumper/jingle detected during transition gap; "
                        f"waiting... ({remaining:.0f}s remaining of '{title}')"
                    )
                    # Wait for it to finish or timeout
                    wait = min(max(remaining, 1.0), max_wait - polled)
                    if wait > 0.5:
                        await asyncio.sleep(wait)
                    polled += wait
                    continue
                # Real song is playing — proceed with broadcast
                return
            except Exception:
                await asyncio.sleep(1.0)
                polled += 1.0
        log_info(
            "[radio_host] Bumper/jingle wait timeout; proceeding with broadcast anyway"
        )

    async def _inject_winding_down_banter(
        self,
        banter_to_inject: dict,
        curr_title: str,
        curr_artist: str,
        song_end_ts: float,
    ) -> None:
        """Background task: TTS + ffmpeg, then broadcast once the song ends
        and any bumper/jingle in the transition gap has finished playing."""
        self._set_animation("speak")
        try:
            # Step 1: generate TTS audio immediately
            audio_path = banter_to_inject.get("audio_path")
            if not audio_path:
                audio_path = await self._injector.generate_tts(banter_to_inject["text"])
            if not audio_path:
                log_error("[radio_host] TTS failed for winding-down banter")
                return

            # Step 2: wait for song end + bumper gap to clear.
            # This replaces the old fixed-timestamp approach which could
            # cause banter to play over AutoDJ bumpers/jingles.
            await self._wait_for_bumper_end(song_end_ts)

            # Step 3: now broadcast into the clean gap.
            # broadcast_banter handles ffmpeg conversion internally.
            synth_name = self._get_synth_name()
            result = await self._client.broadcast_banter(
                station_shortcode=self._station_id,
                audio_path=audio_path,
                username=self._streamer_username,
                password=self._streamer_password,
                title=f"{synth_name} is speaking",
                artist="",
                gain_db=self._gain_db,
            )

            # Mirror the same voice to the WebUI avatar so any connected
            # spectator sees and hears the synth speaking on air (lip-sync +
            # facial expressions), synced with the radio broadcast.
            await self._broadcast_to_webui(audio_path, banter_to_inject["text"])

            await self._log_activity(
                curr_title,
                curr_artist,
                banter_to_inject["text"],
                banter_to_inject.get("style", "transition"),
                result.get("status", "unknown"),
                audio_path=result.get("audio_path"),
            )
        except Exception as e:
            log_error(f"[radio_host] Winding-down injection failed: {e}")
        finally:
            self._set_animation("idle")

    _MAX_PENDING_BANTER = 8

    def _store_pending_banter(self, banter: dict[str, Any]) -> None:
        """Store pre-generated LLM banter keyed by its transition.

        Oldest entries are pruned so stale transitions don't accumulate.
        """
        key = (banter.get("prev_title") or "", banter.get("prev_artist") or "")
        self._pending_banter.pop(key, None)
        self._pending_banter[key] = banter
        while len(self._pending_banter) > self._MAX_PENDING_BANTER:
            self._pending_banter.pop(next(iter(self._pending_banter)))

    def _pop_matching_banter(self, prev_title: str, prev_artist: str) -> dict | None:
        return self._pending_banter.pop((prev_title, prev_artist), None)

    def _enqueue_pre_gen_banter(
        self,
        prev_title: str,
        prev_artist: str,
        curr_title: str,
        curr_artist: str,
    ) -> None:
        asyncio.create_task(
            self._enqueue_banter_generation(
                prev_title=prev_title,
                prev_artist=prev_artist,
                curr_title=curr_title,
                curr_artist=curr_artist,
                pre_generate=True,
                deannounce_only=False,
            )
        )

    async def _generate_banter_on_the_fly(
        self,
        prev_title: str,
        prev_artist: str,
        curr_title: str,
        curr_artist: str,
        deannounce_only: bool = False,
    ) -> dict[str, Any] | None:
        """Enqueue an urgent LLM banter generation and wait for it.

        Returns the banter dict with ``text``, ``style``, and optionally
        ``audio_path``, or None if the LLM did not respond in time.
        """
        await self._enqueue_banter_generation(
            prev_title=prev_title,
            prev_artist=prev_artist,
            curr_title=curr_title,
            curr_artist=curr_artist,
            pre_generate=True,
            deannounce_only=deannounce_only,
        )
        # Poll _pending_banter for up to ~5s waiting for the LLM response
        key = (prev_title, prev_artist)
        for _ in range(20):
            await asyncio.sleep(0.25)
            banter = self._pending_banter.pop(key, None)
            if banter:
                return banter
        return None

    def _set_animation(self, state: str) -> None:
        pm = get_persona_manager()
        if pm:
            asyncio.create_task(pm.set_animation_state(state, session_id=None))

    async def _broadcast_to_webui(self, audio_path: str | None, text: str) -> None:
        """Mirror the radio banter to the WebUI so connected spectators see and
        hear the avatar speak (lip-sync + facial expressions), exactly like a
        normal voice message.  When the synth speaks on air it *is* a voice
        message: anyone in the room should see her talking.

        Best-effort — never blocks or fails the radio broadcast.
        """
        if not audio_path:
            return
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            vox = PLUGIN_REGISTRY.get("vox_plugin") or PLUGIN_REGISTRY.get("vox")
            if vox is None or not hasattr(vox, "broadcast_audio_to_webui"):
                return
            engine_override = (
                str(
                    config_registry.get_value(
                        "RADIO_HOST_VOX_ENGINE",
                        "",
                        value_type=str,
                        group="plugins",
                        component="radio_host",
                    )
                )
                or None
            )
            await vox.broadcast_audio_to_webui(
                audio_path=audio_path,
                text=text,
                engine_name=engine_override,
            )
        except Exception as exc:
            log_warning(f"[radio_host] WebUI mirror failed: {exc}")

    async def _enqueue_banter_generation(
        self,
        prev_title: str,
        prev_artist: str,
        curr_title: str,
        curr_artist: str,
        pre_generate: bool,
        deannounce_only: bool = False,
    ) -> None:
        from core import message_queue

        lang = self._language.strip() or "English"
        desc = self._schedule_desc.strip()
        station = self._station_name.strip()

        if deannounce_only:
            context_text_parts = [
                f"Song that JUST FINISHED: '{prev_title}' by {prev_artist}.",
                "The radio will play the next song automatically — you do NOT "
                "need to introduce it. Just say a quick goodbye to this track.",
            ]
            if station:
                context_text_parts.append(f"You are on {station}.")
            if desc:
                context_text_parts.append(f"Current program: {desc}.")
            synth_profile = str(
                config_registry.get_value("SYNTH_PROFILE", "") or ""
            ).strip()
            if synth_profile:
                context_text_parts.append(f"Your personality: {synth_profile}")
            context_text_parts.extend(
                [
                    f"Write your response in {lang}.",
                    "Generate a short radiophonic de-announcement (1-2 sentences). "
                    "Just mention the song that finished — say it's been great, "
                    "make a quick remark about it, nothing more. "
                    "Be yourself — your personality and mood.",
                    "IMPORTANT: Respond using ONLY the 'radio_speak' action type.",
                ]
            )
        else:
            context_text_parts = [
                f"Song that JUST FINISHED (do NOT say this is playing now): "
                f"'{prev_title}' by {prev_artist}.",
                f"Song that is NOW PLAYING (this is the one to announce): "
                f"'{curr_title}' by {curr_artist}.",
            ]
            if station:
                context_text_parts.append(f"You are on {station}.")
            if desc:
                context_text_parts.append(f"Current program: {desc}.")
            # Include synth personality so the radio DJ voice stays in character
            synth_profile = str(
                config_registry.get_value("SYNTH_PROFILE", "") or ""
            ).strip()
            if synth_profile:
                context_text_parts.append(f"Your personality: {synth_profile}")
            context_text_parts.extend(
                [
                    f"Write your response in {lang}.",
                    "Generate a short DJ transition (1-3 sentences). "
                    "You might say who you are, what you're feeling, "
                    "or make a playful comment according your personality and current mood. "
                    f"Say something about the song NOW PLAYING ('{curr_title}'), "
                    "not the one that just finished. "
                    "Treat jingles, sweepers, and bumpers as non-musical content and ignore them."
                    "Be yourself — your personality, your mood, your sense of humor. "
                    f"NEVER say '{prev_title}' is now playing or coming up next.",
                    f"CRITICAL: Mention the now-playing song ('{curr_title}') by name. "
                    "Do NOT mix up which song is which.",
                    "Add a brief fun fact or curiosity about the artist or song — a notable "
                    "achievement, a sample origin, or an interesting tidbit. "
                    "Keep it to one short sentence.",
                    "IMPORTANT: Respond using ONLY the 'radio_speak' action type. "
                    "Do NOT use 'message_send', 'send_message', 'message', or any other type."
                    "Unless you really want to talk about it with someone in the chat"
                    "But be aware that the user might not be listening to the radio, so please give them context.",
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
        audio_url: str | None = None
        if audio_path:
            # TTS files inside the WebUI static dir can be served directly
            static_prefix = "res/synth_webui/static/"
            if audio_path.startswith(static_prefix):
                audio_url = "/" + audio_path[len("res/") :]
            else:
                # For paths outside the static dir, serve via the API endpoint
                # using a path query param (requires _serve_radio_audio to support it)
                audio_url = f"/api/radio/audio?path={audio_path}"

        self._recent_activities.appendleft(
            {
                "id": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "track_title": track_title,
                "track_artist": track_artist,
                "banter_text": banter_text,
                "style": style or "transition",
                "status": status,
                "audio_url": audio_url,
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
            log_info(
                f"[radio_host] Activity logged: '{track_title}' status={status} "
                f"audio={audio_path or 'none'}"
            )
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
                audio_path = await self._injector.generate_tts(text)
                banter = {
                    "text": text,
                    "style": style,
                    "audio_path": audio_path,
                    "prev_title": context.get("radio_pregenerate_prev_title", ""),
                    "prev_artist": context.get("radio_pregenerate_prev_artist", ""),
                    "curr_title": context.get("radio_pregenerate_next_title", ""),
                    "curr_artist": context.get("radio_pregenerate_next_artist", ""),
                }
                self._store_pending_banter(banter)
                log_info(
                    f"[radio_host] Stored pre-generated banter for "
                    f"'{banter['prev_title']}' -> '{banter['curr_title']}'"
                )
                return {"status": "stored", "output": text}

            # Normal: TTS + upload + log
            self._set_animation("speak")
            try:
                injector_result = await self._injector.inject_banter(text, style)
                # Mirror the same voice to the WebUI avatar so any connected
                # spectator sees and hears the synth speaking on air.
                await self._broadcast_to_webui(injector_result.get("audio_path"), text)
            finally:
                self._set_animation("idle")
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
        """Serve a banter audio file by DB row id (``?id=N``) or direct path (``?path=...``)."""
        from starlette.requests import Request
        from starlette.responses import FileResponse, JSONResponse

        req: Request = request
        row_id = req.query_params.get("id", "")
        direct_path = req.query_params.get("path", "")

        audio_path: str | None = None

        if row_id and row_id.isdigit():
            try:
                from core.db import get_conn_ctx

                async with get_conn_ctx() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT banter_audio_file FROM radio_activity_log WHERE id = %s",
                            (int(row_id),),
                        )
                        row = await cur.fetchone()
                if row:
                    audio_path = (
                        row[0]
                        if isinstance(row, (tuple, list))
                        else row.get("banter_audio_file")
                    )
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
        elif direct_path:
            # Direct path fallback (for in-memory fallback mode, no DB row).
            # Security: only allow paths within known radio audio directories.
            import os as _os

            allowed_prefixes = (
                _os.path.abspath(str(AUDIO_STORAGE_DIR)),
                _os.path.abspath("res/synth_webui/static/audio/tts"),
            )
            abs_path = _os.path.abspath(direct_path)
            for prefix in allowed_prefixes:
                if not prefix:
                    continue
                try:
                    # commonpath (not startswith) so sibling directories like
                    # "<prefix>_other" cannot slip through the check.
                    if _os.path.commonpath([abs_path, prefix]) == prefix:
                        audio_path = direct_path
                        break
                except ValueError:
                    continue

        if not audio_path:
            return JSONResponse({"error": "not found"}, status_code=404)

        import os

        if not os.path.isfile(audio_path):
            return JSONResponse({"error": "file missing"}, status_code=404)
        return FileResponse(audio_path, media_type="audio/wav")

    async def get_live_status(self) -> dict[str, Any] | None:
        """Return the current live radio status for prompt injection / recon.

        Reads the in-memory state held by the running :class:`TrackMonitor`
        (current/next track, listeners, playlist) plus station/schedule info.
        When the monitor has not populated a current track yet, a single live
        ``get_nowplaying`` fetch is attempted as a fallback.

        Returns ``None`` when the radio is disabled or not configured, so the
        caller can cleanly skip injection.
        """
        if not self._enabled or not self._has_runtime_config():
            return None

        online = bool(self._running and self._has_runtime_config())

        current_title: str | None = None
        current_artist: str | None = None
        current_playlist: str = ""
        next_title: str | None = None
        next_artist: str | None = None
        listeners: int | None = None

        if self._monitor is not None:
            current_title = self._monitor.current_track_title
            current_artist = self._monitor.current_track_artist
            current_playlist = self._monitor.current_playlist or ""
            next_title = self._monitor.next_track_title
            next_artist = self._monitor.next_track_artist
            if self._monitor.listener_data_available:
                listeners = self._monitor.current_listeners

        # Fallback: if the monitor has no current track yet, fetch it live once.
        if not current_title and self._client.configured:
            try:
                np = await self._client.get_nowplaying(self._station_id)
                now_playing = (np or {}).get("now_playing") or {}
                song = now_playing.get("song") or {}
                current_title = song.get("title") or current_title
                current_artist = song.get("artist") or current_artist
                current_playlist = now_playing.get("playlist") or current_playlist
                playing_next = (np or {}).get("playing_next") or {}
                next_song = playing_next.get("song") or {}
                next_title = next_song.get("title") or next_title
                next_artist = next_song.get("artist") or next_artist
                listeners_obj = (np or {}).get("listeners") or {}
                if listeners is None and "total" in listeners_obj:
                    listeners = listeners_obj.get("total")
            except Exception as e:
                log_warning(f"[radio_host] get_live_status live fetch failed: {e}")

        return {
            "online": online,
            "enabled": self._enabled,
            "station_name": self._station_name or "",
            "schedule_description": self._schedule_desc or "",
            "current_track_title": current_title or "",
            "current_track_artist": current_artist or "",
            "current_playlist": current_playlist,
            "next_track_title": next_title or "",
            "next_track_artist": next_artist or "",
            "listeners": listeners,
        }

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
