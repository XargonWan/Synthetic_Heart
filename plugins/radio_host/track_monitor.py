from __future__ import annotations

import asyncio
import time
from typing import Callable

from core.logging_utils import log_debug, log_error, log_info

from .azuracast_client import AzuraCastClient

_TRACK_CHANGE_COOLDOWN_S = 10


class TrackMonitor:
    def __init__(
        self,
        client: AzuraCastClient,
        station_id: str,
        poll_interval_s: int = 15,
        intermission: int = 1,
        on_track_change: Callable | None = None,
    ):
        self._client = client
        self._station_id = station_id
        self._poll_interval_s = poll_interval_s
        self._intermission = intermission
        self._on_track_change = on_track_change

        self._last_track_id: str | None = None
        self._last_track_title: str | None = None
        self._last_track_artist: str | None = None
        self._track_count_since_comment = 0
        self._last_change_ts: float = 0.0
        self._running = False
        self._task: asyncio.Task | None = None

        self.current_track_title: str | None = None
        self.current_track_artist: str | None = None
        self.next_track_title: str | None = None
        self.next_track_artist: str | None = None

    def update_config(
        self,
        station_id: str | None = None,
        poll_interval_s: int | None = None,
        intermission: int | None = None,
    ) -> None:
        if station_id is not None:
            self._station_id = station_id
        if poll_interval_s is not None:
            self._poll_interval_s = poll_interval_s
        if intermission is not None:
            self._intermission = intermission

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        log_info("[radio_host] Track monitor started")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        log_info("[radio_host] Track monitor stopped")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._check_track()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_debug(f"[radio_host] Track poll error: {e}")
            await asyncio.sleep(self._poll_interval_s)

    async def _check_track(self) -> None:
        if not self._client.configured or not self._station_id:
            return

        try:
            np = await self._client.get_nowplaying(self._station_id)
        except Exception as e:
            log_debug(f"[radio_host] Nowplaying fetch failed: {e}")
            return

        current = np.get("now_playing", {}) or {}
        track = current.get("song", {}) or {}
        track_id: str | None = str(track.get("id", ""))
        title: str | None = track.get("title")
        artist: str | None = track.get("artist")

        if not track_id or not title or not artist:
            return

        now = time.time()

        if track_id != self._last_track_id:
            elapsed = now - self._last_change_ts
            if self._last_track_id is not None and elapsed >= _TRACK_CHANGE_COOLDOWN_S:
                self._track_count_since_comment += 1
                self._last_track_title = self.current_track_title or title
                self._last_track_artist = self.current_track_artist or artist
                self.current_track_title = title
                self.current_track_artist = artist

                next_song = self._extract_next_song(np)
                self.next_track_title = next_song.get("title")
                self.next_track_artist = next_song.get("artist")

                log_info(
                    f"[radio_host] Track change: "
                    f"'{self._last_track_title}' by {self._last_track_artist} -> "
                    f"'{title}' by {artist}"
                )

                if self._track_count_since_comment >= self._intermission:
                    self._track_count_since_comment = 0
                    await self._fire_track_change()
                else:
                    log_debug(
                        f"[radio_host] Skipping comment "
                        f"({self._track_count_since_comment}/{self._intermission})"
                    )

            self._last_track_id = track_id
            self._last_change_ts = now

    def _extract_next_song(self, np: dict) -> dict[str, str]:
        playing_next = np.get("playing_next", {}) or {}
        song = playing_next.get("song", {}) or {}
        return {
            "title": str(song.get("title", "")),
            "artist": str(song.get("artist", "")),
        }

    async def _fire_track_change(self) -> None:
        if self._on_track_change is None:
            return
        if not self._last_track_title or not self.current_track_title:
            return
        try:
            await self._on_track_change(
                prev_title=self._last_track_title,
                prev_artist=self._last_track_artist,
                curr_title=self.current_track_title,
                curr_artist=self.current_track_artist,
                next_title=self.next_track_title,
                next_artist=self.next_track_artist,
            )
        except Exception as e:
            log_error(f"[radio_host] Track change handler failed: {e}")
