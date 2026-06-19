from __future__ import annotations

import asyncio
import time
from typing import Callable

from core.logging_utils import log_debug, log_error, log_info

from .azuracast_client import AzuraCastClient

_TRACK_CHANGE_COOLDOWN_S = 10
_VERIFY_DELAY_S = 3
_END_OF_SONG_THRESHOLD_S = 45
_JINGLE_MAX_DURATION_S = 45


class TrackMonitor:
    def __init__(
        self,
        client: AzuraCastClient,
        station_id: str,
        poll_interval_s: int = 15,
        intermission: int = 1,
        on_track_change: Callable | None = None,
        on_winding_down: Callable | None = None,
    ):
        self._client = client
        self._station_id = station_id
        self._poll_interval_s = poll_interval_s
        self._intermission = intermission
        self._on_track_change = on_track_change
        self._on_winding_down = on_winding_down

        self._last_track_id: str | None = None
        self._last_track_title: str | None = None
        self._last_track_artist: str | None = None
        self._last_playlist: str | None = None
        self._track_count_since_comment = 0
        self._last_change_ts: float = 0.0
        self._running = False
        self._task: asyncio.Task | None = None

        self.current_track_title: str | None = None
        self.current_track_artist: str | None = None
        self.current_playlist: str = ""
        self.next_track_title: str | None = None
        self.next_track_artist: str | None = None
        self._end_announced_for_id: str | None = None

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
        playlist: str | None = current.get("playlist")

        if not track_id or not title or not artist:
            return

        # Skip metadata-only changes from Synth's own banter (e.g. "SyntH is speaking").
        # These are not real track transitions and must not trigger announcement logic,
        # consume pre-generated banter, or interfere with _last_track_id tracking.
        if "is speaking" in title.lower():
            log_debug(f"[radio_host] Skipping speaking metadata: '{title}' by {artist}")
            return

        now = time.time()

        if track_id != self._last_track_id:
            # Save previous track info BEFORE updating current
            if self._last_track_id is not None:
                self._last_track_title = self.current_track_title
                self._last_track_artist = self.current_track_artist
            else:
                # First detection - no previous track yet
                self._last_track_title = None
                self._last_track_artist = None

            # Saving playlist so we know if this previous track was a jingle
            if self._last_track_id is not None:
                self._last_playlist = self.current_playlist
            else:
                self._last_playlist = None

            # Always update current track info
            self.current_track_title = title
            self.current_track_artist = artist
            self.current_playlist = playlist or ""

            # AzuraCast nowplaying metadata can briefly show a wrong song during
            # transitions/crossfades.  Wait briefly and re-verify so we don't
            # announce a phantom track.
            verified_id = await self._verify_track_stable(track_id)
            if verified_id is None:
                # Verification failed / re-verify the old track — false alarm
                log_info(
                    f"[radio_host] Track change discarded (metadata glitch): "
                    f"'{title}' by {artist}"
                )
                self.current_track_title = self._last_track_title
                self.current_track_artist = self._last_track_artist
                return
            if verified_id != track_id:
                # A different stable track emerged during verification
                track_id = verified_id
                title = self.current_track_title
                artist = self.current_track_artist

            # Re-capture now — the verification sleep above made the original stale
            now = time.time()

            # Fetch the full queue ahead so we can pre-generate multiple transitions
            queue_ahead: list[dict[str, str]] = []
            try:
                queue_data = await self._client.get_station_queue(self._station_id)
                for item in queue_data:
                    song = item.get("song", {}) or {}
                    q_title = str(song.get("title", ""))
                    q_artist = str(song.get("artist", ""))
                    if q_title and q_artist:
                        queue_ahead.append({"title": q_title, "artist": q_artist})
            except Exception:
                pass

            if self._last_track_id is not None:
                # We have a previous track, check if enough time has passed
                elapsed = now - self._last_change_ts
                if elapsed >= _TRACK_CHANGE_COOLDOWN_S:
                    log_info(
                        f"[radio_host] Track change: "
                        f"'{self._last_track_title}' by {self._last_track_artist} -> "
                        f"'{title}' by {artist}"
                    )

                    # Only count real songs (non-jingles) toward the intermission.
                    # Jingles play between real songs and should not advance the
                    # "songs-since-comment" counter, so synth doesn't skip an
                    # announcement just because a 10 s bumper played in between.
                    curr_playlist_lower = self.current_playlist.lower()
                    curr_is_jingle = "jingle" in curr_playlist_lower
                    if not curr_is_jingle:
                        self._track_count_since_comment += 1

                    should_comment = (
                        not curr_is_jingle
                        and self._track_count_since_comment >= self._intermission
                    )
                    if should_comment:
                        self._track_count_since_comment = 0

                    # Next-track info was already refreshed by _verify_track_stable.
                    # Use queue_ahead as fallback when the API field was empty.
                    if not self.next_track_title and queue_ahead:
                        self.next_track_title = queue_ahead[0].get("title")
                        self.next_track_artist = queue_ahead[0].get("artist")

                    await self._fire_track_change(
                        should_comment=should_comment,
                        queue_ahead=queue_ahead,
                        curr_is_jingle=curr_is_jingle,
                    )
                else:
                    if not self.next_track_title and queue_ahead:
                        self.next_track_title = queue_ahead[0].get("title")
                        self.next_track_artist = queue_ahead[0].get("artist")
            else:
                # First track detection - set up initial state
                self._track_count_since_comment = 0

                if not self.next_track_title and queue_ahead:
                    self.next_track_title = queue_ahead[0].get("title")
                    self.next_track_artist = queue_ahead[0].get("artist")

            self._last_track_id = track_id
            self._last_change_ts = now
            # Reset end-of-song tracking for the new track
            self._end_announced_for_id = None

        # End-of-song detection — inject banter BEFORE the current song finishes
        # so it plays during the outro, not over the start of the next track.
        if (
            self._last_track_id is not None
            and self.current_track_title
            and self._end_announced_for_id != self._last_track_id
        ):
            elapsed: float = current.get("elapsed", 0) or 0
            duration: float = current.get("duration", 0) or 0
            remaining = duration - elapsed
            if duration > 0 and 0 <= remaining <= _END_OF_SONG_THRESHOLD_S:
                self._end_announced_for_id = self._last_track_id
                # Skip jingles, bumpers, and other short content — injecting
                # banter during a 10-second station ID sounds wrong.
                playlist = self.current_playlist or ""
                is_short = duration < _JINGLE_MAX_DURATION_S
                is_jingle_playlist = "jingle" in playlist.lower()
                is_bumper_playlist = "bumper" in playlist.lower()
                if is_short or is_jingle_playlist or is_bumper_playlist:
                    log_info(
                        f"[radio_host] Skipping winding down for short content: "
                        f"'{self.current_track_title}' "
                        f"({duration:.0f}s, playlist='{playlist}')"
                    )
                else:
                    log_info(
                        f"[radio_host] Song winding down: "
                        f"'{self.current_track_title}' by {self.current_track_artist} "
                        f"({remaining:.0f}s remaining)"
                    )
                    await self._fire_winding_down(remaining=remaining)

    def _extract_next_song(self, np: dict) -> dict[str, str]:
        playing_next = np.get("playing_next", {}) or {}
        song = playing_next.get("song", {}) or {}
        return {
            "title": str(song.get("title", "")),
            "artist": str(song.get("artist", "")),
        }

    async def _verify_track_stable(self, detected_track_id: str) -> str | None:
        """Wait briefly and re-check the nowplaying metadata.

        AzuraCast's ``/api/nowplaying`` can briefly report the wrong track
        during crossfades / AutoDJ transitions.  After a short sleep we
        re-fetch and check whether the track has settled.

        Also updates ``next_track_title`` / ``next_track_artist`` from the
        fresh data so the caller can use those values immediately.

        Returns the verified track id (which may differ from the original
        detection), or ``None`` if the metadata glitched back to the
        previous song (false alarm).  When verification cannot complete,
        returns *detected_track_id* — never a title, since the caller
        stores the return value as the new ``_last_track_id``.
        """
        await asyncio.sleep(_VERIFY_DELAY_S)
        try:
            np = await self._client.get_nowplaying(self._station_id)
        except Exception:
            return detected_track_id  # proceed with what we have

        vfy_current = np.get("now_playing", {}) or {}
        vfy_track = vfy_current.get("song", {}) or {}
        vfy_id = str(vfy_track.get("id", ""))
        vfy_title: str | None = vfy_track.get("title")
        vfy_artist: str | None = vfy_track.get("artist")

        # Refresh next-track info from the verified response
        next_song = self._extract_next_song(np)
        self.next_track_title = next_song.get("title")
        self.next_track_artist = next_song.get("artist")

        if not vfy_id or not vfy_title or not vfy_artist:
            return detected_track_id  # incomplete data, proceed anyway

        # Skip if the metadata settled back to the previous real song
        if vfy_id == self._last_track_id:
            return None

        # Skip if it settled on our own speaking metadata
        if "is speaking" in vfy_title.lower():
            log_debug(
                "[radio_host] Verify: settled on speaking metadata instead of real track"
            )
            return None

        if vfy_id != self._last_track_id and vfy_title != self.current_track_title:
            log_info(
                f"[radio_host] Track corrected during verification: "
                f"'{self.current_track_title}' -> '{vfy_title}'"
            )
            self.current_track_title = vfy_title
            self.current_track_artist = vfy_artist

        return vfy_id

    async def _fire_track_change(
        self,
        should_comment: bool = True,
        queue_ahead: list[dict[str, str]] | None = None,
        curr_is_jingle: bool = False,
    ) -> None:
        if self._on_track_change is None:
            return
        if not self._last_track_title or not self.current_track_title:
            return
        prev_is_jingle = bool(
            self._last_playlist and "jingle" in self._last_playlist.lower()
        )
        try:
            await self._on_track_change(
                prev_title=self._last_track_title,
                prev_artist=self._last_track_artist,
                curr_title=self.current_track_title,
                curr_artist=self.current_track_artist,
                next_title=self.next_track_title,
                next_artist=self.next_track_artist,
                should_comment=should_comment,
                queue_ahead=queue_ahead,
                prev_is_jingle=prev_is_jingle,
                curr_is_jingle=curr_is_jingle,
            )
        except Exception as e:
            log_error(f"[radio_host] Track change handler failed: {e}")

    async def _fire_winding_down(self, remaining: float = 0) -> None:
        if self._on_winding_down is None:
            return
        if not self.current_track_title:
            return
        try:
            await self._on_winding_down(
                curr_title=self.current_track_title,
                curr_artist=self.current_track_artist,
                next_title=self.next_track_title,
                next_artist=self.next_track_artist,
                remaining=remaining,
            )
        except Exception as e:
            log_error(f"[radio_host] Winding down handler failed: {e}")
