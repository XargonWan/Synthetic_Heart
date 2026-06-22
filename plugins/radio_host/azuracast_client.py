from __future__ import annotations

import asyncio
import json
import time as _time
from pathlib import Path
from typing import Any

import aiohttp
import websockets

from core.logging_utils import log_debug, log_info, log_warning


class AzuraCastError(Exception):
    pass


class AzuraCastClient:
    def __init__(self, base_url: str = "", api_key: str = ""):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        return bool(self._base_url) and bool(self._api_key)

    def update_config(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        if self._session and not self._session.closed:
            try:
                asyncio.get_event_loop().create_task(self._session.close())
            except RuntimeError:
                pass
        self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "X-API-Key": self._api_key,
                    "User-Agent": "SyntheticHeart-RadioHost/1.0",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.configured:
            raise AzuraCastError("AzuraCast not configured")
        session = await self._ensure_session()
        url = f"{self._base_url}{path}"
        log_debug(f"[azuracast] {method} {url}")
        try:
            async with session.request(method, url, **kwargs) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise AzuraCastError(
                        f"HTTP {resp.status} on {method} {path}: {body[:500]}"
                    )
                if resp.status == 204:
                    return {}
                ct = resp.content_type or ""
                if "json" in ct:
                    result = await resp.json()
                    return result if isinstance(result, dict) else {"data": result}
                text = await resp.text()
                return {"text": text}
        except aiohttp.ClientError as e:
            raise AzuraCastError(f"Connection error: {e}") from e

    def _build_webdj_url(self, station_shortcode: str) -> str:
        """Build the WebDJ websocket URL, preserving the base URL's TLS scheme."""
        base = self._base_url
        host = base.split("://", 1)[-1]
        scheme = "wss" if base.lower().startswith("https://") else "ws"
        return f"{scheme}://{host}/webdj/{station_shortcode}/"

    async def get_nowplaying(self, station_id: str | None = None) -> dict[str, Any]:
        if station_id:
            return await self._request("GET", f"/api/nowplaying/{station_id}")
        data = await self._request("GET", "/api/nowplaying")
        stations = data.get("data", [data]) if isinstance(data, dict) else data
        if isinstance(stations, list) and stations:
            return stations[0]
        return data

    async def update_nowplaying_metadata(
        self, station_id: str, artist: str, title: str, album: str | None = None
    ) -> bool:
        payload: dict[str, str] = {"artist": artist, "title": title}
        if album:
            payload["album"] = album
        try:
            await self._request(
                "POST",
                f"/api/station/{station_id}/nowplaying/update",
                json=payload,
            )
            log_info(f"[azuracast] Metadata updated: {artist} - {title}")
            return True
        except AzuraCastError as e:
            log_warning(f"[azuracast] Failed to update metadata: {e}")
            return False

    async def get_station_name(self, station_id: str) -> str:
        np = await self.get_nowplaying(station_id)
        station = np.get("station", {}) or {}
        return str(station.get("name", "") or "")

    async def get_station_queue(self, station_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/station/{station_id}/queue")
        return data.get("data", [data]) if isinstance(data, dict) else data

    async def get_station_schedule(self, station_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/station/{station_id}/schedule")
        raw = data.get("data", [data]) if isinstance(data, dict) else data
        return raw if isinstance(raw, list) else []

    async def broadcast_banter(
        self,
        station_shortcode: str,
        audio_path: str,
        username: str = "SyntH",
        password: str = "synthradio",
        title: str = "",
        artist: str = "",
        gain_db: float = 4.0,
    ) -> dict[str, Any]:
        webm_data = await self._convert_to_webm(audio_path, gain_db=gain_db)
        if webm_data is None:
            return {"status": "error", "reason": "conversion_failed"}

        ws_url = self._build_webdj_url(station_shortcode)

        log_info(
            f"[azuracast] Broadcasting {len(webm_data)}b WebM via WebDJ ({username})"
        )
        try:
            async with websockets.connect(
                ws_url, subprotocols=["webcast"], close_timeout=30
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "data": {
                                "mime": "audio/webm;codecs=opus",
                                "user": username,
                                "password": password,
                            },
                        }
                    )
                )

                if title or artist:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "metadata",
                                "data": {
                                    "title": title or "Synth Radio",
                                    "artist": artist or "Synthetic Heart",
                                },
                            }
                        )
                    )

                # Send audio immediately — the caller should have already
                # waited for the AutoDJ→LiveDJ transition timing.
                chunk_size = 4096
                for i in range(0, len(webm_data), chunk_size):
                    await ws.send(webm_data[i : i + chunk_size])
                    await asyncio.sleep(0.05)

                await asyncio.sleep(2)

            log_info("[azuracast] WebDJ broadcast finished (AutoDJ resumes)")
            return {"status": "success"}
        except Exception as e:
            log_warning(f"[azuracast] WebDJ broadcast error: {e}")
            return {"status": "error", "reason": str(e)}

    async def convert_audio_to_webm(
        self, audio_path: str, gain_db: float = 4.0
    ) -> bytes | None:
        """Public wrapper: convert a WAV file to WebM bytes."""
        return await self._convert_to_webm(audio_path, gain_db=gain_db)

    async def broadcast_webm_at(
        self,
        webm_data: bytes,
        station_shortcode: str,
        song_end_ts: float,
        username: str = "SyntH",
        password: str = "synthradio",
        title: str = "",
        artist: str = "",
    ) -> dict[str, Any]:
        """Connect WebDJ and broadcast pre-converted audio precisely at *song_end_ts*.

        The caller should have already waited until ~2 s before *song_end_ts*.
        This method connects, then waits the remaining delta so that audio
        starts flowing **exactly** at song end.  The AutoDJ→LiveDJ transition
        completes during the last 2 s of the previous song, leaving a clean
        gap for the announcement with no jingle overlap.
        """
        ws_url = self._build_webdj_url(station_shortcode)

        log_info(f"[azuracast] Timed broadcast: {len(webm_data)}b WebM at song_end")
        try:
            async with websockets.connect(
                ws_url, subprotocols=["webcast"], close_timeout=30
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "data": {
                                "mime": "audio/webm;codecs=opus",
                                "user": username,
                                "password": password,
                            },
                        }
                    )
                )

                if title or artist:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "metadata",
                                "data": {
                                    "title": title or "Synth Radio",
                                    "artist": artist or "Synthetic Heart",
                                },
                            }
                        )
                    )

                # Wait until the exact song end time
                now = _time.time()
                remaining_delta = song_end_ts - now
                if remaining_delta > 0:
                    await asyncio.sleep(remaining_delta)

                # Song just ended — send audio into the clean gap
                chunk_size = 4096
                for i in range(0, len(webm_data), chunk_size):
                    await ws.send(webm_data[i : i + chunk_size])
                    await asyncio.sleep(0.05)

                await asyncio.sleep(1)

            log_info("[azuracast] Timed WebDJ broadcast finished (AutoDJ resumes)")
            return {"status": "success"}
        except Exception as e:
            log_warning(f"[azuracast] Timed WebDJ broadcast error: {e}")
            return {"status": "error", "reason": str(e)}

    async def _convert_to_webm(
        self, input_path: str, gain_db: float = 4.0
    ) -> bytes | None:
        path_obj = Path(input_path)
        if not path_obj.exists():
            log_warning(f"[azuracast] Audio file not found: {input_path}")
            return None
        try:
            # Apply dynamic normalization + explicit gain boost.
            # dynaudnorm handles overall loudness, volume applies the user-configured dB gain.
            audio_filter = f"dynaudnorm=f=150:g=15:p=0.95,volume={gain_db}dB"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-i",
                input_path,
                "-af",
                audio_filter,
                "-f",
                "webm",
                "-c:a",
                "libopus",
                "-b:a",
                "48k",
                "-ar",
                "48000",
                "-ac",
                "1",
                "-loglevel",
                "warning",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode(errors="replace")[:300]
                log_warning(f"[azuracast] ffmpeg conversion failed: {err}")
                return None
            if not stdout:
                log_warning("[azuracast] ffmpeg produced empty output")
                return None
            return stdout
        except FileNotFoundError:
            log_warning("[azuracast] ffmpeg not found; cannot convert audio")
            return None
        except Exception as e:
            log_warning(f"[azuracast] ffmpeg error: {e}")
            return None
