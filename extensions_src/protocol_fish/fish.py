"""Fish Audio cloud TTS (https://api.fish.audio/v1/tts), as an endpoint protocol.

Fish Audio is a TTS-only provider: it synthesises speech and reports a fixed ``vox``
capability. Requests use the Fish payload ``{"text", "reference_id", "format"}`` with the API
key as an ``Authorization: Bearer`` header and the model tier (e.g. ``s2.1-pro-free``) as a
``model`` header.

Relevant ``extra_config`` keys (set by the provider preset or the add-endpoint wizard):

* ``tts_model``         — model tier header (default ``s2.1-pro-free``)
* ``tts_output_format`` — ``wav`` / ``mp3`` / ``pcm`` payload format
* ``tts_reference_id``  — cloned/library voice id used as ``reference_id``
* ``tts_extra_payload`` — optional dict merged into the request payload
* ``manual_voices``     — voices the operator added by URL

It replaces ``core/external_endpoints/adapters/fish_audio_adapter.py``, removed in commit
16154870 (``git log --diff-filter=D -- core/external_endpoints/adapters/fish_audio_adapter.py``).
"""

from __future__ import annotations

import time as _time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

DEFAULT_BASE_URL = "https://api.fish.audio/v1/tts"
DEFAULT_MODEL = "s2.1-pro-free"

# Fish Audio model *tiers* (the ``model`` header): synthesis engine tiers, not voices. They
# fill the WebUI model dropdown. Voices ("reference IDs") come from ``GET /model``.
MODEL_TIERS: tuple[tuple[str, str], ...] = (
    ("s2.1-pro-free", "Speech 2.1 Pro (free tier)"),
    ("s2.1-pro", "Speech 2.1 Pro"),
    ("s1", "Speech 1"),
)
SUPPORTED_FORMATS = frozenset({"wav", "mp3", "pcm"})

# Latency budget for one synthesis request. A 2-3k character reply on the free tier measured
# 14s-44s upstream, and an earlier 60s cap timed out on the long ones: the engine returned
# nothing and the reply went out as plain text, a duplicate bubble.
REQUEST_TIMEOUT_SEC = 180


class Log:
    """Where this module reports; ``ext_main`` points it at ``ctx.log``."""

    target: Any = None

    @classmethod
    def warning(cls, message: str) -> None:
        if cls.target is not None:
            cls.target.warning(message)


class FishError(Exception):
    pass


def models() -> list[dict[str, Any]]:
    return [
        {
            "id": tier_id,
            "name": tier_label,
            "owned_by": "fish.audio",
            "capabilities": {"vox": True},
            "model_type": "tts",
            "input_modalities": ["text"],
            "output_modalities": ["audio"],
        }
        for tier_id, tier_label in MODEL_TIERS
    ]


def parse_model_id_from_url(url_or_id: str) -> str | None:
    """A Fish Audio ``modelId`` from a share URL or a bare id.

    Accepts ``https://fish.audio/app/text-to-speech/?modelId=<id>`` (or any Fish URL carrying
    a ``modelId``/``model_id`` query parameter) and a bare reference id.
    """
    value = str(url_or_id or "").strip()
    if not value:
        return None
    if "://" not in value and "?" not in value and "/" not in value:
        return value  # looks like a bare reference id
    try:
        parsed = urlparse(value)
        qs = parse_qs(parsed.query)
        for key in ("modelId", "model_id", "id"):
            if key in qs and qs[key]:
                candidate = qs[key][0].strip()
                if candidate:
                    return candidate
        segments = [seg for seg in parsed.path.split("/") if seg]
        if segments:
            return segments[-1]
    except Exception:  # noqa: BLE001
        return None
    return None


class Fish:
    """The Fish Audio endpoint of one request."""

    def __init__(self, endpoint: dict[str, Any], api_key: str) -> None:
        self._base_url = str(endpoint.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._api_key = api_key
        extra = endpoint.get("extra_config")
        self._extra: dict[str, Any] = extra if isinstance(extra, dict) else {}

    def need_key(self) -> None:
        if not self._api_key:
            raise FishError("this endpoint requires an API key")

    # -------------------------------------------------------------------- tts

    def _format(self, requested: Any) -> str:
        fmt = str(
            requested
            or self._extra.get("tts_output_format")
            or self._extra.get("output_format")
            or "wav"
        ).lower()
        return fmt if fmt in SUPPORTED_FORMATS else "wav"

    async def tts(
        self, text: str, voice: str | None, kwargs: dict[str, Any]
    ) -> bytes | None:
        reference_id = str(
            voice
            or self._extra.get("tts_reference_id")
            or self._extra.get("reference_id")
            or ""
        ).strip()
        fmt = self._format(kwargs.get("format"))
        model = str(
            kwargs.get("model") or self._extra.get("tts_model") or DEFAULT_MODEL
        )
        payload: dict[str, Any] = {"text": text, "format": fmt}
        if reference_id:
            payload["reference_id"] = reference_id
        extra_payload = self._extra.get("tts_extra_payload")
        if isinstance(extra_payload, dict):
            for key, value in extra_payload.items():
                payload.setdefault(str(key), value)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "model": model,
        }
        started = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
                resp = await client.post(self._base_url, json=payload, headers=headers)
            if resp.status_code != 200:
                Log.warning(
                    f"[fish_audio] {self._base_url} returned HTTP {resp.status_code}: "
                    f"{resp.text[:500]}"
                )
                return None
            return resp.content
        except Exception as exc:  # noqa: BLE001
            elapsed = _time.monotonic() - started
            # Timeouts stringify to "": name the type so the cause is not hidden.
            reason = str(exc) or f"{type(exc).__name__} (no message)"
            Log.warning(
                f"[fish_audio] request failed after {elapsed:.1f}s "
                f"(limit {REQUEST_TIMEOUT_SEC}s): {reason}"
            )
            return None

    # ----------------------------------------------------------------- voices

    def _api_root(self) -> str:
        """The API root (``https://api.fish.audio``) from the ``/v1/tts`` base URL."""
        root = self._base_url
        for suffix in ("/v1/tts", "/tts", "/v1"):
            if root.endswith(suffix):
                root = root[: -len(suffix)]
                break
        return root.rstrip("/") or "https://api.fish.audio"

    def _manual_voices(self) -> list[dict[str, Any]]:
        raw = self._extra.get("manual_voices")
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            ref_id = str(item.get("reference_id") or "").strip()
            if ref_id:
                out.append(
                    {
                        "reference_id": ref_id,
                        "title": str(item.get("title") or ref_id),
                        "language": item.get("language"),
                    }
                )
        return out

    async def _page(self, params: dict[str, str]) -> list[dict[str, Any]]:
        url = f"{self._api_root()}/model"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    params=params,
                )
            if resp.status_code != 200:
                Log.warning(
                    f"[fish_audio] list_speakers HTTP {resp.status_code}: {resp.text[:300]}"
                )
                return []
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            Log.warning(f"[fish_audio] list_speakers request failed: {exc}")
            return []
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        speakers: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ref_id = item.get("_id") or item.get("id")
            title = item.get("title") or item.get("name") or ref_id
            if not ref_id:
                continue
            languages = item.get("languages") or []
            speakers.append(
                {
                    "reference_id": str(ref_id),
                    "title": str(title),
                    "language": languages[0]
                    if isinstance(languages, list) and languages
                    else None,
                }
            )
        return speakers

    async def list_speakers(self) -> list[dict[str, Any]]:
        """The available voices, ordered by tier: the operator's manual ones, the account's
        own cloned voices, then the popular public library (alphabetical within a tier;
        de-duplicated, the highest tier wins). Bookmarks cannot be listed through the API."""
        tiers: list[tuple[int, str, list[dict[str, Any]]]] = [
            (1, "Manually added", self._manual_voices()),
            (2, "My Voices", await self._page({"self": "true", "page_size": "100"})),
            (
                3,
                "Default Voices",
                await self._page({"sort_by": "task_count", "page_size": "100"}),
            ),
        ]
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tier, label, entries in tiers:
            for entry in sorted(
                entries, key=lambda e: str(e.get("title") or "").lower()
            ):
                ref_id = str(entry.get("reference_id") or "").strip()
                if not ref_id or ref_id in seen:
                    continue
                seen.add(ref_id)
                entry["tier"] = tier
                entry["tier_label"] = label
                merged.append(entry)
        return merged

    async def fetch_model_detail(self, model_id: str) -> dict[str, Any] | None:
        """Metadata of one voice, to add it by its shared URL."""
        model_id = str(model_id or "").strip()
        if not model_id:
            return None
        url = f"{self._api_root()}/model/{model_id}"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {self._api_key}"}
                )
            if resp.status_code != 200:
                Log.warning(
                    f"[fish_audio] fetch_model_detail HTTP {resp.status_code}: {resp.text[:300]}"
                )
                return None
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            Log.warning(f"[fish_audio] fetch_model_detail request failed: {exc}")
            return None
        if not isinstance(data, dict):
            return None
        ref_id = data.get("_id") or data.get("id") or model_id
        title = data.get("title") or data.get("name") or ref_id
        languages = data.get("languages") or []
        return {
            "reference_id": str(ref_id),
            "title": str(title),
            "language": languages[0]
            if isinstance(languages, list) and languages
            else None,
        }
