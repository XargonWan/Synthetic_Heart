# core/external_endpoints/adapters/fish_audio_adapter.py
"""Adapter for the Fish Audio cloud TTS API (https://api.fish.audio/v1/tts).

Fish Audio is a TTS-only provider: the adapter implements ``generate_tts``
and reports a fixed ``vox`` capability.  Requests use the Fish payload schema
``{"text", "reference_id", "format"}`` with the API key sent as an
``Authorization: Bearer`` header and the model tier (e.g. ``s2.1-pro-free``)
as a ``model`` header.

Relevant ``extra_config`` keys (set by the provider preset / add-endpoint
wizard):

* ``tts_model``        — model tier header (default ``s2.1-pro-free``)
* ``tts_output_format``— ``wav`` / ``mp3`` / ``pcm`` payload format
* ``tts_reference_id`` — cloned/library voice id used as ``reference_id``
* ``tts_extra_payload``— optional dict merged into the request payload
  (e.g. ``{"temperature": 0.7}`` prosody controls)
"""

from __future__ import annotations

import time as _time
from typing import Any

import aiohttp

from core.cortex_api_logger import log_cortex_request, log_cortex_response
from core.external_endpoints.adapters.base import (
    BaseProtocolAdapter,
    ChatResponse,
    ModelInfo,
)
from core.logging_utils import log_warning

DEFAULT_BASE_URL = "https://api.fish.audio/v1/tts"
DEFAULT_MODEL = "s2.1-pro-free"

# Fish Audio model *tiers* (the ``model`` header) — these are synthesis engine
# tiers, not voices. They are advertised statically to populate the WebUI model
# dropdown. Actual voices ("reference IDs") are fetched dynamically via the
# ``GET /model`` voice-listing endpoint (see ``list_speakers``).
_MODEL_TIERS: tuple[tuple[str, str], ...] = (
    ("s2.1-pro-free", "Speech 2.1 Pro (free tier)"),
    ("s2.1-pro", "Speech 2.1 Pro"),
    ("s1", "Speech 1"),
)

_SUPPORTED_FORMATS = frozenset({"wav", "mp3", "pcm"})


class FishAudioAdapter(BaseProtocolAdapter):
    """Adapter for the Fish Audio ``/v1/tts`` endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._api_key = api_key
        self._extra_config = extra_config or {}

    # ------------------------------------------------------------------
    # Chat / LLM — not supported (TTS-only provider)
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatResponse:
        raise NotImplementedError("Fish Audio is a TTS-only endpoint")

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=tier_id,
                name=tier_label,
                owned_by="fish.audio",
                capabilities={"vox": True},
                model_type="tts",
                input_modalities=["text"],
                output_modalities=["audio"],
            )
            for tier_id, tier_label in _MODEL_TIERS
        ]

    # ------------------------------------------------------------------
    # TTS
    # ------------------------------------------------------------------

    def _resolve_format(self, requested: Any) -> str:
        fmt = str(
            requested
            or self._extra_config.get("tts_output_format")
            or self._extra_config.get("output_format")
            or "wav"
        ).lower()
        return fmt if fmt in _SUPPORTED_FORMATS else "wav"

    async def generate_tts(
        self,
        text: str,
        voice: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        engine_tag = f"fish_audio:{self._engine_label or 'default'}"

        reference_id = str(
            voice
            or self._extra_config.get("tts_reference_id")
            or self._extra_config.get("reference_id")
            or ""
        ).strip()
        fmt = self._resolve_format(kwargs.get("format"))
        model = str(
            kwargs.get("model") or self._extra_config.get("tts_model") or DEFAULT_MODEL
        )

        payload: dict[str, Any] = {"text": text, "format": fmt}
        if reference_id:
            payload["reference_id"] = reference_id
        extra_payload = self._extra_config.get("tts_extra_payload")
        if isinstance(extra_payload, dict):
            for key, value in extra_payload.items():
                payload.setdefault(str(key), value)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "model": model,
        }

        log_cortex_request(
            engine_tag,
            model=model,
            url=self._base_url,
            payload={
                "task": "generate_tts",
                "text_length": len(text),
                "reference_id": reference_id or None,
                "format": fmt,
            },
        )
        _req_start = _time.monotonic()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._base_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:500]
                        _elapsed = (_time.monotonic() - _req_start) * 1000
                        log_cortex_response(
                            engine_tag,
                            model=model,
                            status=resp.status,
                            error=f"HTTP {resp.status}: {body}",
                            elapsed_ms=_elapsed,
                        )
                        log_warning(
                            f"[fish_audio] {self._base_url} returned "
                            f"HTTP {resp.status}: {body}"
                        )
                        return None
                    audio_data = await resp.read()
                    _elapsed = (_time.monotonic() - _req_start) * 1000
                    log_cortex_response(
                        engine_tag,
                        model=model,
                        status=200,
                        body=f"<audio: {len(audio_data)} bytes>",
                        elapsed_ms=_elapsed,
                    )
                    return audio_data
        except Exception as exc:
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model=model,
                error=str(exc),
                elapsed_ms=_elapsed,
            )
            log_warning(f"[fish_audio] request failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Probe / health
    # ------------------------------------------------------------------

    def _api_root(self) -> str:
        """Derive the Fish Audio API root (``https://api.fish.audio``) from the
        ``/v1/tts`` base URL."""
        root = self._base_url
        for suffix in ("/v1/tts", "/tts", "/v1"):
            if root.endswith(suffix):
                root = root[: -len(suffix)]
                break
        return root.rstrip("/") or "https://api.fish.audio"

    async def list_speakers(self) -> list[dict[str, Any]]:
        """Return the endpoint's available voices, ordered by hierarchy tier.

        Fish Audio exposes a voice/model catalogue at ``https://api.fish.audio/
        model``. Each entry has an ``_id`` used as the synthesis ``reference_id``
        plus a human ``title`` and optional ``languages``. The picker composes
        the list from three tiers (alphabetical within each, tier precedence
        first):

        1. **Manually added** — voices the user added by URL (persisted in
           ``extra_config["manual_voices"]``).
        2. **My Voices** — the account's own cloned/custom voices (``self=true``).
        3. **Default Voices** — the popular public library.

        Note: the Fish Audio REST ``GET /model`` endpoint exposes no filter for
        a user's *bookmarked* voices (only ``self``, ``title``, ``tag``,
        ``language``, ``author_id`` and ``sort_by``). Bookmarks are a
        web-app-only feature and cannot be listed via the API, so there is no
        Bookmarks tier — users pin specific voices through the "Manage Voices"
        panel (tier 1) instead.

        Each entry carries ``tier`` (int, lower = higher precedence) and
        ``tier_label`` so the WebUI can group and order them. De-duplicates by
        ``reference_id`` keeping the highest-precedence (lowest-tier) occurrence.
        Returns ``{"reference_id", "title", "language", "tier", "tier_label"}``
        dicts (empty on total failure).
        """
        url = f"{self._api_root()}/model"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        tiers: list[tuple[int, str, list[dict[str, Any]]]] = []

        # Tier 1 — manually added voices (from extra_config, no network call).
        tiers.append((1, "Manually added", self._manual_voices()))

        # Tier 2 — the account's own cloned/custom voices.
        tiers.append(
            (
                2,
                "My Voices",
                await self._fetch_model_page(
                    url, headers, {"self": "true", "page_size": "100"}
                ),
            )
        )

        # Tier 3 — the popular public library (default voices).
        tiers.append(
            (
                3,
                "Default Voices",
                await self._fetch_model_page(
                    url, headers, {"sort_by": "task_count", "page_size": "100"}
                ),
            )
        )

        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tier, label, entries in tiers:
            entries_sorted = sorted(
                entries, key=lambda e: str(e.get("title") or "").lower()
            )
            for entry in entries_sorted:
                ref_id = str(entry.get("reference_id") or "").strip()
                if not ref_id or ref_id in seen:
                    continue
                seen.add(ref_id)
                entry["tier"] = tier
                entry["tier_label"] = label
                merged.append(entry)
        return merged

    def _manual_voices(self) -> list[dict[str, Any]]:
        """Return the user's manually-added voices from ``extra_config``.

        Stored as ``extra_config["manual_voices"]`` — a list of
        ``{"reference_id", "title", "language"}`` dicts added via the WebUI
        "Manage Voices" panel.
        """
        raw = self._extra_config.get("manual_voices")
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            ref_id = str(item.get("reference_id") or "").strip()
            if not ref_id:
                continue
            out.append(
                {
                    "reference_id": ref_id,
                    "title": str(item.get("title") or ref_id),
                    "language": item.get("language"),
                }
            )
        return out

    async def fetch_model_detail(self, model_id: str) -> dict[str, Any] | None:
        """Fetch metadata for a single Fish Audio voice by its ``model_id``.

        Used by the WebUI "Manage Voices" panel to scrape a voice's ``title``
        and ``language`` from a shared fish.audio URL before persisting it.
        Returns ``{"reference_id", "title", "language"}`` or ``None`` on failure.
        """
        model_id = str(model_id or "").strip()
        if not model_id:
            return None
        url = f"{self._api_root()}/model/{model_id}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        log_warning(
                            f"[fish_audio] fetch_model_detail HTTP {resp.status}: {body}"
                        )
                        return None
                    data = await resp.json()
        except Exception as exc:
            log_warning(f"[fish_audio] fetch_model_detail request failed: {exc}")
            return None

        if not isinstance(data, dict):
            return None
        ref_id = data.get("_id") or data.get("id") or model_id
        title = data.get("title") or data.get("name") or ref_id
        languages = data.get("languages") or []
        language = languages[0] if isinstance(languages, list) and languages else None
        return {
            "reference_id": str(ref_id),
            "title": str(title),
            "language": language,
        }

    @staticmethod
    def parse_model_id_from_url(url_or_id: str) -> str | None:
        """Extract a Fish Audio ``modelId`` from a share URL or bare id.

        Accepts URLs like
        ``https://fish.audio/app/text-to-speech/?modelId=<id>`` (or any Fish
        URL carrying a ``modelId``/``model_id`` query param) as well as a bare
        reference id. Returns the id, or ``None`` if nothing usable is found.
        """
        value = str(url_or_id or "").strip()
        if not value:
            return None
        if "://" not in value and "?" not in value and "/" not in value:
            # Looks like a bare reference id.
            return value
        try:
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(value)
            qs = parse_qs(parsed.query)
            for key in ("modelId", "model_id", "id"):
                if key in qs and qs[key]:
                    candidate = qs[key][0].strip()
                    if candidate:
                        return candidate
            # Fall back to a trailing path segment (e.g. /model/<id>).
            segments = [seg for seg in parsed.path.split("/") if seg]
            if segments:
                return segments[-1]
        except Exception:
            return None
        return None

    async def _fetch_model_page(
        self,
        url: str,
        headers: dict[str, str],
        params: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Fetch and parse a single ``GET /model`` page into speaker dicts."""
        speakers: list[dict[str, Any]] = []
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        log_warning(
                            f"[fish_audio] list_speakers HTTP {resp.status}: {body}"
                        )
                        return []
                    data = await resp.json()
        except Exception as exc:
            log_warning(f"[fish_audio] list_speakers request failed: {exc}")
            return []

        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        for item in items:
            if not isinstance(item, dict):
                continue
            ref_id = item.get("_id") or item.get("id")
            title = item.get("title") or item.get("name") or ref_id
            if not ref_id:
                continue
            languages = item.get("languages") or []
            language = (
                languages[0] if isinstance(languages, list) and languages else None
            )
            speakers.append(
                {
                    "reference_id": str(ref_id),
                    "title": str(title),
                    "language": language,
                }
            )
        return speakers

    async def probe_capabilities(self) -> dict[str, bool]:
        # TTS-only provider; synthesis is billed, so no live probe request.
        return {"cortex": False, "vox": True, "auris": False, "live": False}

    async def health_check(self) -> bool:
        # No free health endpoint — reachable iff configured with a key.
        return bool(self._api_key)

    async def ping_test(
        self,
        model: str | None = None,
        timeout: float = 15.0,
    ) -> tuple[bool, str]:
        return False, "Fish Audio is a TTS-only endpoint (no chat ping)"
