"""The radio host: an AI DJ for an AzuraCast station.

It watches the station's now-playing data, asks the model (through ``ctx.turns``) for a
short spoken remark around each song change, has the synth's voice say it
(``ctx.voice``) and streams it into the station through the WebDJ connection, timed to
land in the gap between songs. The same clip is played on the avatar's stage so anyone
watching sees the synth speak. Everything the Core owns (the model, the voice, the
avatar, the chat history) is reached only through those calls.

It replaces the in-tree plugin ``plugins/radio_host``, removed in commit 713590cd; the old
code is found with ``git log --diff-filter=D -- plugins/radio_host/radio_host_plugin.py``.
"""

from __future__ import annotations

import asyncio
import re
import time as _time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from azuracast_client import AzuraCastClient
from jingle import KEEP_REPLAYS, JingleInjector
from radiolog import log_error, log_info, log_warning
from synth_sdk.runtime import Context, WebRequest, WebResponse
from track_monitor import TrackMonitor

INTERFACE_ID = "radio_host"
#: The one conversation of the radio: it is nobody's chat, so it has a fixed id.
INTERNAL_CHAT_ID = -2
RADIO_PATH = f"{INTERFACE_ID}/{INTERNAL_CHAT_ID}"
SPEAK = "radio_speak"
UPDATE_METADATA = "radio_update_metadata"
RECON_KEY = "radio_status"
ACTIVITY_KEY = "activities"
MAX_ACTIVITIES = 40

# On-the-fly de-announce generation runs through the model (LLM + TTS), which
# routinely takes 30-45s end-to-end. Poll long enough to actually capture our own
# generation instead of timing out.
_ON_THE_FLY_POLL_TIMEOUT_S = 60.0
# The winding-down on-the-fly generation runs as a background task (never
# blocking the track monitor) and waits longer than the immediate-injection
# poll above, because a slow LLM turn routinely exceeds 60s and the freshness
# guard makes a late, stale banter safe to skip.
_ON_THE_FLY_BACKGROUND_TIMEOUT_S = 120.0
_ON_THE_FLY_POLL_INTERVAL_S = 0.25
#: How often the settings are read again (there is no change notification).
CONFIG_POLL_S = 20.0

SPEAK_ACTION = {
    "description": (
        "Output your spoken DJ commentary on the radio stream. Use THIS action (not "
        "message_send or send_message) when you want to say something on air. "
        "Generate a short radio DJ transition comment (1-3 sentences). Be natural and "
        "match your personality. You can reference the current mood, memories, or "
        "recent conversations from your context."
    ),
    "fields": {
        "text": {"type": "string", "description": "The spoken comment (1-3 sentences)"},
        "style": {
            "type": "string",
            "description": "transition, intro, outro, news or shoutout; 'transition' for between-song banter",
            "optional": True,
        },
    },
}
UPDATE_METADATA_ACTION = {
    "description": (
        "Update the now-playing metadata on the radio stream with the current song's "
        "artist and title."
    ),
    "fields": {
        "artist": {"type": "string", "description": "Artist name"},
        "title": {"type": "string", "description": "Song title"},
        "album": {
            "type": "string",
            "description": "Optional album name",
            "optional": True,
        },
    },
}

_TRUE_WORDS = ("true", "yes", "1")


def is_relevant(answer: Any) -> bool:
    """Interpret the model's verdict for the ``radio_status`` question."""
    if isinstance(answer, bool):
        return answer
    if isinstance(answer, dict):
        answer = answer.get(RECON_KEY)
        if isinstance(answer, bool):
            return answer
    if isinstance(answer, str):
        return answer.strip().lower() in _TRUE_WORDS
    return False


def format_status(status: dict[str, Any]) -> str:
    if not status.get("online"):
        station = status.get("station_name") or "the radio"
        return f"Live radio status: {station} is currently OFFLINE (not broadcasting)."

    lines: list[str] = ["Live radio status:"]
    station = status.get("station_name")
    if station:
        lines.append(f"- Station: {station}")

    title = status.get("current_track_title")
    artist = status.get("current_track_artist")
    if title or artist:
        if title and artist:
            lines.append(f"- Now playing: {artist} - {title}")
        else:
            lines.append(f"- Now playing: {title or artist}")
    else:
        lines.append("- Now playing: unknown (no track data available yet)")

    next_title = status.get("next_track_title")
    next_artist = status.get("next_track_artist")
    if next_title or next_artist:
        if next_title and next_artist:
            lines.append(f"- Up next: {next_artist} - {next_title}")
        else:
            lines.append(f"- Up next: {next_title or next_artist}")

    listeners = status.get("listeners")
    if isinstance(listeners, int):
        lines.append(f"- Listeners: {listeners}")

    schedule = status.get("schedule_description")
    if schedule:
        lines.append(f"- Schedule: {schedule}")

    return "\n".join(lines)


class RadioHost:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._client = AzuraCastClient()
        self._audio_dir = ctx.data_dir / "audio"
        self._injector = JingleInjector(
            ctx, self._client, "", audio_dir=self._audio_dir
        )
        self._monitor: TrackMonitor | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._config_task: asyncio.Task[None] | None = None
        self._config_seen: dict[str, Any] = {}
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
        self._track_count_since_comment = 0
        self._station_info_ts: float = 0.0
        self._STATION_INFO_TTL = 300
        self._station_name = ""
        self._schedule_desc = ""
        self._synth_name = "SyntH"
        self._recent_activities: deque[dict[str, Any]] = deque(maxlen=MAX_ACTIVITIES)
        self._activity_seq = 0
        self._background: set[asyncio.Task[Any]] = set()
        # settings (see ``_apply_config``)
        self._station_id = ""
        self._poll_interval = 15
        self._intermission = 1
        self._listener_history = 5
        self._language = "English"
        self._gain_db = 4.0
        self._next_song_announcement = False
        self._on_the_fly_timeout_s = _ON_THE_FLY_BACKGROUND_TIMEOUT_S
        self._announce_if_no_listeners = True
        self._streamer_username = "SyntH"
        self._streamer_password = "synthradio"

    # ------------------------------------------------------------- settings

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        """A background task that is kept alive and reported when it fails."""
        task = asyncio.ensure_future(coro)
        self._background.add(task)

        def done(t: asyncio.Task[Any]) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log_error(f"[radio_host] background task failed: {t.exception()!r}")

        task.add_done_callback(done)
        return task

    def _apply_config(self, values: dict[str, Any]) -> None:
        def text(key: str, default: str = "") -> str:
            return str(values.get(key) if values.get(key) is not None else default)

        def number(key: str, default: float, kind: Any = float) -> Any:
            try:
                return kind(
                    values.get(key) if values.get(key) not in (None, "") else default
                )
            except (TypeError, ValueError):
                return kind(default)

        base_url = text("AZURACAST_BASE_URL")
        api_key = text("AZURACAST_API_KEY")
        self._station_id = text("AZURACAST_STATION_ID")
        self._poll_interval = number("RADIO_HOST_POLL_INTERVAL_S", 15, int)
        self._intermission = number("RADIO_HOST_INTERMISSION", 1, int)
        self._listener_history = number("RADIO_HOST_LISTENER_HISTORY", 5, int)
        self._language = text("RADIO_HOST_LANGUAGE", "English")
        self._gain_db = number("RADIO_HOST_GAIN_DB", 4.0)
        self._next_song_announcement = bool(
            values.get("RADIO_HOST_NEXT_SONG_ANNOUNCEMENT")
        )
        self._on_the_fly_timeout_s = (
            number("RADIO_HOST_ON_THE_FLY_TIMEOUT_S", _ON_THE_FLY_BACKGROUND_TIMEOUT_S)
            or _ON_THE_FLY_BACKGROUND_TIMEOUT_S
        )
        announce = values.get("RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS")
        self._announce_if_no_listeners = True if announce is None else bool(announce)
        self._streamer_username = (
            text("AZURACAST_STREAMER_USERNAME", "SyntH") or "SyntH"
        )
        self._streamer_password = (
            text("AZURACAST_STREAMER_PASSWORD", "synthradio") or "synthradio"
        )
        self._client.update_config(base_url, api_key)
        self._injector.update_station_shortcode(self._station_id)
        self._injector.update_gain(self._gain_db)
        self._injector.update_voice_engine(text("RADIO_HOST_VOX_ENGINE"))
        self._injector.update_streamer_credentials(
            username=self._streamer_username, password=self._streamer_password
        )
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

    async def _watch_config(self) -> None:
        """Pick up changed settings: restart the monitor when the station changed."""
        while not self.ctx.stopping.is_set():
            try:
                await asyncio.wait_for(self.ctx.stopping.wait(), CONFIG_POLL_S)
                return
            except asyncio.TimeoutError:
                pass
            try:
                values = await self.ctx.config.all()
            except Exception as exc:  # noqa: BLE001 - keep the radio on its old settings
                log_warning(f"[radio_host] could not read settings: {exc}")
                continue
            if values == self._config_seen:
                continue
            self._config_seen = dict(values)
            was_running = self._running
            self._apply_config(values)
            log_info("[radio_host] settings changed")
            if was_running:
                await self._ensure_stopped()
            await self._ensure_running()

    # ----------------------------------------------------------- life cycle

    async def start(self) -> None:
        self._config_seen = dict(await self.ctx.config.all())
        self._apply_config(self._config_seen)
        try:
            info = await self.ctx.core.get()
            self._synth_name = str(info.get("synth_name") or "SyntH")
        except Exception:  # noqa: BLE001 - the name is only decoration
            pass
        await self._load_activities()
        await self._ensure_running()
        self._config_task = self._spawn(self._watch_config())
        log_info("[radio_host] RadioHost initialized")

    async def stop(self) -> None:
        if self._config_task is not None:
            self._config_task.cancel()
            self._config_task = None
        await self._ensure_stopped()
        for task in list(self._background):
            task.cancel()
        await self._client.close()
        log_info("[radio_host] RadioHost stopped")

    def _get_synth_name(self) -> str:
        return self._synth_name

    def _has_runtime_config(self) -> bool:
        return bool(self._station_id) and self._client.configured

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
            self._spawn(
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
            self._spawn(
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
        """Pre-generate banter for upcoming transitions from queue data.

        No-op when ``RADIO_HOST_NEXT_SONG_ANNOUNCEMENT`` is disabled: in
        de-announce mode the winding-down handler always generates its own
        de-announce on the fly, so pre-generated *transition* banter is
        never wanted.  Worse, transition banter is keyed by its ``from``
        track ``(prev_title, prev_artist)`` — the same key the de-announce
        generator polls — so a leftover transition entry would be popped
        and broadcast, airing an announcement of the NEXT song while the
        current one is still playing.
        """
        if not self._next_song_announcement:
            return
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

        result = await self._injector.inject_banter(
            banter_to_inject["text"],
            banter_to_inject.get("style", "transition"),
            pre_generated_audio_path=banter_to_inject.get("audio_path"),
            synth_name=self._get_synth_name(),
        )
        await self._finish_banter(
            result,
            banter_to_inject["text"],
            banter_to_inject.get("style", "transition"),
            curr_title,
            curr_artist,
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
                # Run generation + timed injection as a background task so the
                # track monitor's poll loop is never blocked by the (potentially
                # slow) LLM turn, and so a slow-but-recoverable turn is still
                # captured instead of failing the old fixed 60 s poll.
                self._spawn(
                    self._generate_and_inject_winding_down_banter(
                        prev_title=curr_title,
                        prev_artist=curr_artist,
                        curr_title=actual_next,
                        curr_artist=actual_next_artist,
                        deannounce_only=False,
                        song_end_ts=_time.time() + remaining,
                    )
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
            # Run generation + timed injection as a background task so the
            # track monitor's poll loop is never blocked by the (potentially
            # slow) LLM turn, and so a slow-but-recoverable turn is still
            # captured instead of failing the old fixed 60 s poll.
            self._spawn(
                self._generate_and_inject_winding_down_banter(
                    prev_title=curr_title,
                    prev_artist=curr_artist,
                    curr_title="",
                    curr_artist="",
                    deannounce_only=True,
                    song_end_ts=_time.time() + remaining,
                )
            )
            return

        # Clear the fallback flag — we just injected
        self._inject_at_track_change = False

        # Compute the absolute timestamp when the song will end
        song_end_ts = _time.time() + remaining

        # Fire the injection pipeline as a background task.  The pipeline
        # generates TTS, then waits for the song to end + any bumper/jingle
        # in the transition gap, then broadcasts in the clean gap.
        self._spawn(
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

    async def _resolve_clean_gap_end_ts(self, song_end_ts: float) -> float:
        """Return the timestamp of the next *clean* transition gap.

        The winding-down song ends at *song_end_ts*, but AzuraCast/AutoDJ
        frequently drops a short bumper/jingle straight into that gap.  If we
        start streaming the announcement exactly at *song_end_ts* it overlaps
        the jingle and the beginning of the speech is cut off (the listener
        only hears the second half).

        This polls nowplaying and, if a bumper/jingle is bridging the gap,
        returns the timestamp when it finishes — i.e. the start of the *next*
        clean gap — so the broadcaster can target that instead.  When no
        bumper is present it returns *song_end_ts* unchanged.  Fails safe by
        returning *song_end_ts* on any error or timeout so a legitimate
        de-announce is never suppressed.
        """
        max_wait = 30.0
        polled = 0.0
        while polled < max_wait:
            try:
                np = await self._client.get_nowplaying(self._station_id)
                current = np.get("now_playing", {}) or {}
                track = current.get("song", {}) or {}
                title = track.get("title", "") or ""
                if "is speaking" in title.lower():
                    # Our own banter is already on air — target the current
                    # gap so we don't stack another announcement on top.
                    return _time.time()
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
                        f"[radio_host] Bumper/jingle in transition gap; "
                        f"deferring broadcast to its end "
                        f"({remaining:.0f}s remaining of '{title}')"
                    )
                    # The clean gap opens when this bumper finishes.
                    return _time.time() + max(remaining, 0.0)
                # A real (non-bumper) track is already on air — the clean gap
                # is now, target immediately rather than wait.
                return _time.time()
            except Exception:
                await asyncio.sleep(1.0)
                polled += 1.0
        log_info("[radio_host] Bumper/jingle check timeout; broadcasting at song end")
        return song_end_ts

    async def _inject_winding_down_banter(
        self,
        banter_to_inject: dict,
        curr_title: str,
        curr_artist: str,
        song_end_ts: float,
    ) -> None:
        """Background task: TTS + ffmpeg, then broadcast timed so the LiveDJ
        connects *before* the current song ends and the audio flows into the
        clean AutoDJ->LiveDJ gap.

        Connecting after the song has already ended (the old approach) let the
        AutoDJ start the *next* track first; the LiveDJ connection then cut it
        off mid-play and, on disconnect, the AutoDJ resumed from the track
        *after* that -- effectively skipping a song.  Connecting a couple of
        seconds before song end makes the transition land in the natural gap so
        no queued track is skipped.
        """
        audio_path: str | None = None
        try:
            # Step 1: generate TTS audio immediately
            audio_path = banter_to_inject.get("audio_path")
            if not audio_path:
                audio_path = await self._injector.generate_tts(banter_to_inject["text"])
            if not audio_path:
                log_error("[radio_host] TTS failed for winding-down banter")
                return

            # Step 2: convert to WebM up front so the WebDJ connection can start
            # streaming the instant the song ends, with no conversion latency in
            # the critical transition window.
            webm_data = await self._client.convert_audio_to_webm(
                audio_path, gain_db=self._gain_db
            )
            if webm_data is None:
                log_error("[radio_host] WebM conversion failed for winding-down banter")
                return
            replay_path = self._injector.save_replay(webm_data)

            # Step 3: TTS + conversion (and, for on-the-fly de-announces, the
            # LLM generation itself) take a variable amount of time.  If they
            # took longer than the winding-down window, the song we set out to
            # de-announce may have already finished AND the next track already
            # started.  De-announcing then would land in the wrong gap and
            # reference a song the listener stopped hearing tracks ago (e.g. it
            # would de-announce a track while a completely different, later song
            # is on air).  Detect that and abandon the injection instead of
            # deferring it to the end of the *next* song.
            if await self._deannounce_target_expired(curr_title):
                log_info(
                    f"[radio_host] Skipping winding-down injection for "
                    f"'{curr_title}': its transition gap already passed "
                    f"(a later track is now on air)"
                )
                return

            # Step 3b: TTS + conversion take a variable amount of time, so the
            # song_end_ts computed at winding-down detection is now stale.
            # Re-fetch nowplaying to recompute how long is actually left on
            # the currently-playing song, so the broadcast lands in the real
            # gap rather than an estimate made before generation started.
            song_end_ts = await self._recompute_song_end_ts(song_end_ts)

            # Step 3c: the target song ends at song_end_ts, but AzuraCast often
            # drops a short bumper/jingle straight into that gap.  Streaming
            # exactly at song_end_ts would overlap the jingle and cut off the
            # start of the speech (listener hears only the second half).  Wait
            # for the song to actually end, then resolve the *clean* gap that
            # opens after any bumper/jingle, and target that instead.
            now = _time.time()
            if song_end_ts > now:
                await asyncio.sleep(song_end_ts - now)
            song_end_ts = await self._resolve_clean_gap_end_ts(song_end_ts)

            # Step 4: wait until ~2 s before the clean gap opens, then hand off
            # to the timed broadcaster which connects the WebDJ and starts
            # streaming exactly at the gap -- before the next queued track has
            # begun and after any bridging jingle has finished.
            lead_time_s = 2.0
            now = _time.time()
            connect_at = song_end_ts - lead_time_s
            if connect_at > now:
                await asyncio.sleep(connect_at - now)

            synth_name = self._get_synth_name()
            result = await self._client.broadcast_webm_at(
                webm_data=webm_data,
                station_shortcode=self._station_id,
                song_end_ts=song_end_ts,
                username=self._streamer_username,
                password=self._streamer_password,
                title=f"{synth_name} is speaking",
                artist="",
            )

            await self._finish_banter(
                {
                    "status": result.get("status", "unknown"),
                    "audio_path": audio_path,
                    "replay_path": replay_path,
                },
                banter_to_inject["text"],
                banter_to_inject.get("style", "transition"),
                curr_title,
                curr_artist,
            )
        except Exception as e:
            log_error(f"[radio_host] Winding-down injection failed: {e}")

    async def _recompute_song_end_ts(self, fallback_song_end_ts: float) -> float:
        """Re-derive when the currently-playing song will end.

        TTS generation and audio conversion take a variable amount of time, so
        the ``song_end_ts`` computed when winding-down was first detected is
        stale by the time we are ready to broadcast.  Re-fetch nowplaying and
        recompute ``now + remaining`` from the live ``elapsed``/``duration`` of
        the track that is actually playing.

        Returns the refreshed timestamp, or ``fallback_song_end_ts`` if
        nowplaying is unavailable or does not expose usable timing.
        """
        try:
            np = await self._client.get_nowplaying(self._station_id)
            current = np.get("now_playing", {}) or {}
            remaining = current.get("remaining")
            if remaining is None:
                duration = current.get("duration", 0) or 0
                elapsed = current.get("elapsed", 0) or 0
                remaining = duration - elapsed
            remaining = float(remaining)
            if remaining <= 0:
                # Song already ended (or timing unusable); broadcast now.
                return _time.time()
            return _time.time() + remaining
        except Exception as e:
            log_warning(f"[radio_host] Could not recompute song end time: {e}")
            return fallback_song_end_ts

    async def _deannounce_target_expired(self, target_title: str) -> bool:
        """Return True if *target_title* is no longer the track whose end we
        are about to fill.

        A de-announce is only meaningful in the gap right after the target
        song ends.  If banter generation (LLM + TTS + conversion) outran the
        winding-down window, the target song has already finished *and* a
        later track is on air.  We compare the live ``now_playing`` title
        against the target: if a different, non-bumper song is playing, the
        gap is gone and we should abandon the injection rather than defer it
        to the end of the current (wrong) track.

        Returns False (proceed) when nowplaying is unavailable or when the
        target is still on air / a short bumper is bridging the gap, so a
        transient fetch failure never suppresses a legitimate de-announce.
        """
        if not target_title:
            return False
        try:
            np = await self._client.get_nowplaying(self._station_id)
            current = np.get("now_playing", {}) or {}
            song = current.get("song", {}) or {}
            current_title = str(song.get("title", "") or "")
            if not current_title or current_title == target_title:
                # Still on the target song (or timing unknown) — proceed.
                return False
            # A different track is on air.  If it is a short bumper/jingle
            # bridging the gap, the de-announce still lands correctly, so keep
            # going; otherwise the real next song has begun and the gap is gone.
            playlist = str(current.get("playlist", "") or "").lower()
            duration = float(current.get("duration", 0) or 0)
            is_short = 0 < duration < 45
            is_bumper = "bumper" in playlist or "jingle" in playlist
            if is_short or is_bumper:
                return False
            return True
        except Exception as e:
            log_warning(
                f"[radio_host] Could not verify de-announce target freshness: {e}"
            )
            return False

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
        self._spawn(
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
        # Poll _pending_banter waiting for the LLM response.  The banter is
        # generated asynchronously through the message queue / beat pipeline,
        # which routinely takes 30-45s end-to-end (LLM + TTS).  A short poll
        # (the previous ~5s) always timed out, so the de-announce silently
        # failed.  Wait long enough to actually capture our own generation.
        key = (prev_title, prev_artist)
        for _ in range(int(_ON_THE_FLY_POLL_TIMEOUT_S / _ON_THE_FLY_POLL_INTERVAL_S)):
            await asyncio.sleep(_ON_THE_FLY_POLL_INTERVAL_S)
            banter = self._pending_banter.pop(key, None)
            if banter:
                return banter
        return None

    async def _generate_and_inject_winding_down_banter(
        self,
        prev_title: str,
        prev_artist: str,
        curr_title: str,
        curr_artist: str,
        deannounce_only: bool,
        song_end_ts: float,
    ) -> None:
        """Enqueue an on-the-fly banter generation and inject it when ready.

        Runs as a background task so the track monitor's poll loop is never
        blocked by the (potentially slow) LLM turn.  Polls ``_pending_banter``
        for up to ``RADIO_HOST_ON_THE_FLY_TIMEOUT_S`` seconds — long enough to
        capture a slow-but-recoverable turn that the old fixed 60 s poll always
        missed — then hands off to the timed winding-down injection, which
        already guards against a stale transition
        (``_deannounce_target_expired``), so a late generation is safely
        skipped rather than mis-broadcast.
        """
        await self._enqueue_banter_generation(
            prev_title=prev_title,
            prev_artist=prev_artist,
            curr_title=curr_title,
            curr_artist=curr_artist,
            pre_generate=True,
            deannounce_only=deannounce_only,
        )
        key = (prev_title, prev_artist)
        timeout_s = max(float(self._on_the_fly_timeout_s or 0.0), 0.0)
        waited = 0.0
        while self._running and waited < timeout_s:
            await asyncio.sleep(_ON_THE_FLY_POLL_INTERVAL_S)
            waited += _ON_THE_FLY_POLL_INTERVAL_S
            banter = self._pending_banter.pop(key, None)
            if banter:
                self._inject_at_track_change = False
                await self._inject_winding_down_banter(
                    banter_to_inject=banter,
                    curr_title=prev_title,
                    curr_artist=prev_artist,
                    song_end_ts=song_end_ts,
                )
                return
        if self._running:
            log_error(
                "[radio_host] On-the-fly LLM banter generation timed out; "
                "skipping winding-down injection"
            )

    # --------------------------------------------------------- on air, logged

    async def _finish_banter(
        self,
        result: dict[str, Any],
        text: str,
        style: str,
        track_title: str,
        track_artist: str,
    ) -> None:
        """After a clip went on air: show it on the stage, log it, tidy up.

        The synth is speaking on air, so anyone watching the stage sees and hears it too
        (lip-sync and expressions come from the Core). Best-effort: never fails the
        broadcast that already happened.
        """
        wav = result.get("audio_path")
        if wav:
            try:
                await self.ctx.voice.play(wav, text, self._injector._voice_engine)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                log_warning(f"[radio_host] stage mirror failed: {exc}")
        await self._log_activity(
            track_title,
            track_artist,
            text,
            style,
            result.get("status", "unknown"),
            audio_file=Path(result["replay_path"]).name
            if result.get("replay_path")
            else None,
        )
        self._injector.forget(wav)

    async def _load_activities(self) -> None:
        try:
            stored = await self.ctx.storage.get(ACTIVITY_KEY, [])
            seq = await self.ctx.storage.get("activity_seq", 0)
        except Exception as exc:  # noqa: BLE001
            log_warning(f"[radio_host] could not read the activity log: {exc}")
            return
        if isinstance(stored, list):
            self._recent_activities = deque(
                (a for a in stored if isinstance(a, dict)), maxlen=MAX_ACTIVITIES
            )
        self._activity_seq = int(seq) if isinstance(seq, int) else 0

    async def _log_activity(
        self,
        track_title: str,
        track_artist: str,
        banter_text: str,
        style: str,
        status: str,
        audio_file: str | None = None,
    ) -> None:
        self._activity_seq += 1
        self._recent_activities.appendleft(
            {
                "id": self._activity_seq,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "track_title": track_title[:300],
                "track_artist": track_artist[:300],
                "banter_text": banter_text[:1500],
                "style": style or "transition",
                "status": status,
                "audio_file": audio_file,
            }
        )
        log_info(
            f"[radio_host] Activity logged: '{track_title}' status={status} "
            f"audio={audio_file or 'none'}"
        )
        try:
            await self.ctx.storage.set(ACTIVITY_KEY, list(self._recent_activities))
            await self.ctx.storage.set("activity_seq", self._activity_seq)
        except Exception as exc:  # noqa: BLE001 - the window then shows this run's list
            log_warning(f"[radio_host] Failed to store the activity log: {exc}")
        removed = self._injector.trim_replays(KEEP_REPLAYS)
        for entry in self._recent_activities:
            if removed and entry.get("audio_file") in removed:
                entry["audio_file"] = None

    # ----------------------------------------------------- asking the model

    async def _enqueue_banter_generation(
        self,
        prev_title: str,
        prev_artist: str,
        curr_title: str,
        curr_artist: str,
        pre_generate: bool,
        deannounce_only: bool = False,
    ) -> None:
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

        # What comes back with the model's ``radio_speak`` action (see ``_run_speak``).
        context: dict[str, Any] = {
            "current_track_title": curr_title[:300],
            "current_track_artist": curr_artist[:300],
        }
        if pre_generate:
            context.update(
                {
                    "radio_pre_generating": True,
                    "radio_pregenerate_prev_title": prev_title[:300],
                    "radio_pregenerate_prev_artist": prev_artist[:300],
                    "radio_pregenerate_next_title": curr_title[:300],
                    "radio_pregenerate_next_artist": curr_artist[:300],
                }
            )
        try:
            await self.ctx.turns.request(
                INTERFACE_ID,
                RADIO_PATH,
                context_text,
                allowed_actions=[SPEAK],
                sender_name="Radio Host",
                priority="elevated",
                context=context,
                history_recent_max=self._listener_history,
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

    # -------------------------------------------------------------- actions

    async def handle_action(
        self,
        interface_id: str,
        name: str,
        payload: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if interface_id != INTERFACE_ID:
            return {"status": "failed", "message": "not this extension's interface"}
        if name == SPEAK:
            return await self._run_speak(payload, context)
        if name == UPDATE_METADATA:
            artist = str(payload.get("artist") or "")
            title = str(payload.get("title") or "")
            album = payload.get("album")
            if artist and title and self._client.configured:
                ok = await self._client.update_nowplaying_metadata(
                    self._station_id,
                    artist,
                    title,
                    album if isinstance(album, str) else None,
                )
                return {"status": "success" if ok else "failed"}
            return {"status": "skipped", "message": "missing fields"}
        return {"status": "failed", "message": f"Unknown action: {name}"}

    async def _run_speak(
        self, payload: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        text = str(
            payload.get("text") or payload.get("body") or payload.get("content") or ""
        )
        style = str(payload.get("style") or "transition")
        if not text.strip():
            return {"status": "skipped", "message": "empty_text"}

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
            return {"status": "success", "message": "stored"}

        # Normal: TTS + upload + log
        injector_result = await self._injector.inject_banter(
            text, style, synth_name=self._get_synth_name()
        )
        await self._finish_banter(
            injector_result,
            text,
            style,
            str(context.get("current_track_title", "")),
            str(context.get("current_track_artist", "")),
        )
        if injector_result.get("status") == "success":
            return {"status": "success", "message": text[:200]}
        return {
            "status": "failed",
            "message": str(
                injector_result.get("reason") or injector_result.get("status")
            ),
        }

    async def on_send_message(
        self, interface_id: str, payload: dict[str, Any], origin: dict[str, Any]
    ) -> bool:
        """The radio does not chat: it speaks through ``radio_speak``."""
        log_warning("[radio_host] send_message is not supported; use radio_speak")
        return False

    # ---------------------------------------------------------------- recon

    async def on_contribute(
        self, key: str, answer: Any, text: str, interface_path: str
    ) -> list[dict[str, Any]]:
        """Live status for the prompt when the conversation is about the radio."""
        if key != RECON_KEY or not is_relevant(answer):
            return []
        status = await self.get_live_status()
        if not status:
            return []
        log_info("[radio_host] Injecting live radio status into prompt")
        return [{"content": format_status(status)}]

    async def get_live_status(self) -> dict[str, Any] | None:
        """The current live radio status for prompt injection (``None`` when the
        radio is not configured, so the caller can cleanly skip it)."""
        if not self._has_runtime_config():
            return None

        online = bool(self._running)

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
            "station_name": self._station_name or "",
            "schedule_description": self._schedule_desc or "",
            "current_track_title": current_title or "",
            "current_track_artist": current_artist or "",
            "current_playlist": current_playlist,
            "next_track_title": next_title or "",
            "next_track_artist": next_artist or "",
            "listeners": listeners,
        }

    # ------------------------------------------------------------- Radio window

    async def on_web_request(self, request: WebRequest) -> WebResponse:
        if request.method != "GET":
            return WebResponse(405, {"error": "method not allowed"})
        if request.path.rstrip("/") == "data":
            return WebResponse(200, await self._build_radio_data())
        if request.path.rstrip("/") == "audio":
            return self._serve_radio_audio(request.query.get("id", ""))
        return WebResponse(404, {"error": "not found"})

    def _serve_radio_audio(self, raw_id: str) -> WebResponse:
        """A replay clip by activity id; only files this extension wrote are served."""
        if not raw_id.isdigit():
            return WebResponse(404, {"error": "not found"})
        wanted = int(raw_id)
        name = next(
            (
                a.get("audio_file")
                for a in self._recent_activities
                if a.get("id") == wanted
            ),
            None,
        )
        if not name or not re.fullmatch(r"banter_\d+\.webm", str(name)):
            return WebResponse(404, {"error": "not found"})
        path = self._audio_dir / str(name)
        try:
            data = path.read_bytes()
        except OSError:
            return WebResponse(404, {"error": "file missing"})
        return WebResponse(
            200,
            data,
            {"content-type": "audio/webm", "cache-control": "private, max-age=3600"},
        )

    async def _build_radio_data(self) -> dict[str, Any]:
        activities = [
            {
                "id": a.get("id"),
                "timestamp": str(a.get("timestamp") or ""),
                "track_title": str(a.get("track_title") or ""),
                "track_artist": str(a.get("track_artist") or ""),
                "banter_text": str(a.get("banter_text") or ""),
                "style": str(a.get("style") or "transition"),
                "status": str(a.get("status") or ""),
                "audio_url": f"api/audio?id={a.get('id')}"
                if a.get("audio_file")
                else None,
            }
            for a in self._recent_activities
        ]
        return {
            "configured": self._has_runtime_config(),
            "online": self._running and self._has_runtime_config(),
            "poll_interval": self._poll_interval,
            "intermission": self._intermission,
            "language": self._language,
            "station_name": self._station_name or "",
            "schedule_description": self._schedule_desc or "",
            "station_id": self._station_id or "",
            "base_url": self._client._base_url if self._client.configured else "",  # noqa: SLF001
            "activities": activities,
        }
