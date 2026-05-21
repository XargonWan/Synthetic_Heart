from __future__ import annotations

from typing import Any

import aiohttp

from core.logging_utils import log_debug, log_info, log_warning


class AzuraCastError(Exception):
    pass


class AzuraCastClient:
    def __init__(self, base_url: str = "", api_key: str = ""):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._last_track_id: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self._base_url) and bool(self._api_key)

    def update_config(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # Close stale session so next request picks up the new credentials
        if self._session and not self._session.closed:
            import asyncio

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

    async def get_nowplaying(self, station_id: str | None = None) -> dict[str, Any]:
        if station_id:
            return await self._request("GET", f"/api/nowplaying/{station_id}")
        data = await self._request("GET", "/api/nowplaying")
        stations = data.get("data", [data]) if isinstance(data, dict) else data
        if isinstance(stations, list) and stations:
            return stations[0]
        return data

    async def get_station_queue(self, station_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/station/{station_id}/queue")
        return data.get("data", [data]) if isinstance(data, dict) else data

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

    async def upload_file(
        self, station_id: str, file_path: str, destination: str | None = None
    ) -> dict[str, Any] | None:
        dest = destination or file_path.split("/")[-1]
        session = await self._ensure_session()
        url = f"{self._base_url}/api/station/{station_id}/files"
        try:
            with open(file_path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field(
                    "file", f, filename=dest.split("/")[-1], content_type="audio/wav"
                )
                form.add_field("path", dest)
                form.add_field("storageLocation", "local")
                async with session.post(url, data=form) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        log_warning(
                            f"[azuracast] Upload failed: HTTP {resp.status}: {body[:300]}"
                        )
                        return None
                    result = await resp.json()
                    log_info(f"[azuracast] File uploaded: {dest}")
                    return result
        except (aiohttp.ClientError, OSError) as e:
            log_warning(f"[azuracast] Upload error: {e}")
            return None

    async def get_media_id(self, station_id: str, filename: str) -> int | None:
        try:
            data = await self._request(
                "GET",
                f"/api/station/{station_id}/files",
                params={"search": filename, "per_page": 5},
            )
            files = data.get("data", []) if isinstance(data, dict) else data
            if isinstance(files, list):
                for f in files:
                    if isinstance(f, dict):
                        path = f.get("path", "") or f.get("filename", "")
                        if filename in path:
                            return f.get("id") or f.get("unique_id")
        except AzuraCastError:
            pass
        return None

    async def get_playlists(self, station_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/station/{station_id}/playlists")
        return data.get("data", [data]) if isinstance(data, dict) else data

    async def get_playlist_songs(
        self, station_id: str, playlist_id: int
    ) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            f"/api/station/{station_id}/playlist/{playlist_id}",
        )
        songs = data.get("data", [data]) if isinstance(data, dict) else data
        if isinstance(songs, list):
            return songs
        return data.get("songs", []) if isinstance(data, dict) else []

    async def get_requests(self, station_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/station/{station_id}/requests")
        return data.get("data", [data]) if isinstance(data, dict) else data

    async def get_station_schedule(self, station_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/station/{station_id}/schedule")
        raw = data.get("data", [data]) if isinstance(data, dict) else data
        return raw if isinstance(raw, list) else []

    async def get_station_name(self, station_id: str) -> str:
        np = await self.get_nowplaying(station_id)
        station = np.get("station", {}) or {}
        return str(station.get("name", "") or "")

    async def queue_media(self, station_id: str, media_unique_id: str) -> bool:
        """Queue a media item for playback in the station's AutoDJ queue."""
        try:
            await self._request(
                "POST",
                f"/api/station/{station_id}/queue",
                json={"media": media_unique_id},
            )
            log_info(f"[azuracast] Queued media {media_unique_id} for playback")
            return True
        except AzuraCastError as e:
            log_warning(f"[azuracast] Failed to queue media: {e}")
            return False

    async def delete_media(self, station_id: str, media_id: int | str) -> bool:
        """Delete a media file from station storage by its ID."""
        try:
            await self._request(
                "DELETE",
                f"/api/station/{station_id}/files/{media_id}",
            )
            log_info(f"[azuracast] Deleted media {media_id}")
            return True
        except AzuraCastError as e:
            log_warning(f"[azuracast] Failed to delete media {media_id}: {e}")
            return False
