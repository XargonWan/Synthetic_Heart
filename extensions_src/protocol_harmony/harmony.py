"""Harmony AI Cloud (project-harmony.ai): the parts that are not OpenAI-shaped.

Harmony is OpenAI-compatible for chat, embeddings and vision (the Core's own OpenAI
adapter answers those: this protocol names ``base = "openai"``), but its **model listing**
and its **audio endpoints** are its own:

* ``GET /v1/models`` is public and returns rich descriptors OpenAI never emits:
  ``model_type`` (``llm`` / ``tts`` / ``stt`` / ``embeddings`` / ...),
  ``input_modalities`` / ``output_modalities`` arrays and a ``speech_options`` object
  (``languages`` / ``voices`` / cloning support). The canonical id field is ``model_id``.
* ``POST /v1/audio/speech`` (TTS) takes a **JSON** body with a required ``mode``
  (``single_speaker_tts`` / ``voice_cloning``) and an ``output_options.format`` selector,
  and returns the audio **base64 in a JSON** ``data`` field. ``language`` / ``voice`` are
  only valid for models that advertise them; cloning models take ``input_audio`` /
  ``input_embedding`` instead.
* ``POST /v1/audio/transcriptions`` (STT) also takes **JSON**, with the audio base64 in
  ``input_audio``, and returns the transcript in ``text``.

It replaces ``core/external_endpoints/adapters/harmony_ai_adapter.py``, removed in commit
16154870 (``git log --diff-filter=D -- core/external_endpoints/adapters/harmony_ai_adapter.py``).
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

# Default synthesis mode when the caller does not specify one. KittenTTS and OpenVoice
# support single-speaker; cloning-only models require mode="voice_cloning" plus a reference.
TTS_MODE = "single_speaker_tts"
# Default output container requested via ``output_options.format``.
TTS_FORMAT = "mp3"

_CAPABILITY_ALIASES = {
    "supportsvision": "vision",
    "supportsimageinput": "vision",
    "supportsimage": "vision",
    "supportsimages": "vision",
    "supportsmultipleimages": "vision",
    "supportsvideoinput": "vision",
}


class Warn:
    """Where this module reports; ``ext_main`` points it at ``ctx.log``."""

    log: Any = None

    @classmethod
    def warning(cls, message: str) -> None:
        if cls.log is not None:
            cls.log.warning(message)


class HarmonyError(Exception):
    pass


# ------------------------------------------------------------------ parsing


def as_str_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).lower() for item in value if item is not None]
    if isinstance(value, str) and value:
        return [value.lower()]
    return []


def capability_flags(capabilities: Any) -> dict[str, bool]:
    """Declared capability payload as lowercase ``{name: bool}`` (booleans only)."""
    if isinstance(capabilities, dict):
        return {
            str(key).lower(): value
            for key, value in capabilities.items()
            if isinstance(key, (str, int, float)) and isinstance(value, bool)
        }
    if isinstance(capabilities, (list, tuple, set)):
        return {
            str(item).lower(): True
            for item in capabilities
            if isinstance(item, (str, int, float))
        }
    if isinstance(capabilities, (str, int, float)):
        return {str(capabilities).lower(): True}
    return {}


def normalize_capabilities(*sources: Any) -> dict[str, bool]:
    """Union every declared capability source and fold known aliases (true wins)."""
    flags: dict[str, bool] = {}
    for source in sources:
        for name, declared in capability_flags(source).items():
            flags[name] = flags.get(name, False) or declared
    for declared_name, subsystem in _CAPABILITY_ALIASES.items():
        if flags.get(declared_name):
            flags[subsystem] = True
    return flags


def parse_languages(entry: dict[str, Any]) -> list[dict[str, str]]:
    """Supported languages of a Harmony model entry.

    Handles ``speech_options.languages`` = ``[{"code","name"}, ...]`` and a flat
    ``languages`` list of codes. Deterministic: no keyword matching.
    """
    raw: Any = None
    speech = entry.get("speech_options")
    if isinstance(speech, dict):
        raw = speech.get("languages")
    if raw is None:
        raw = entry.get("languages")
    result: list[dict[str, str]] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                code = str(item.get("code", "") or "")
                if code:
                    result.append(
                        {"code": code, "name": str(item.get("name", code) or code)}
                    )
            elif isinstance(item, str) and item:
                result.append({"code": item, "name": item})
    return result


def derive_subsystems(
    *, model_type: str, input_modalities: list[str], output_modalities: list[str]
) -> dict[str, bool]:
    """Map Harmony's structured model metadata onto SyntH subsystems.

    Purely structural (model_type + modality arrays), never keyword-based, so it works
    across languages and naming conventions.
    """
    mt = (model_type or "").lower()
    in_mods, out_mods = set(input_modalities), set(output_modalities)
    caps: dict[str, bool] = {}
    if mt == "llm" or ("text" in in_mods and "text" in out_mods):
        caps["cortex"] = True
    if mt == "tts" or ("text" in in_mods and "audio" in out_mods):
        caps["vox"] = True
    if mt == "stt" or ("audio" in in_mods and "text" in out_mods):
        caps["auris"] = True
    if "image" in in_mods or "video" in in_mods:
        caps["vision"] = True
    return caps


def parse_model_entry(entry: Any) -> dict[str, Any]:
    """One ``/v1/models`` entry as the Core's model descriptor."""
    if not isinstance(entry, dict):
        entry = {}
    # Harmony's canonical id field is ``model_id``; fall back to ``id``.
    entry_id = str(entry.get("model_id", "") or entry.get("id", "") or "")
    declared = normalize_capabilities(entry.get("capabilities", {}))
    model_type = str(entry.get("model_type", "") or "")
    input_modalities = as_str_list(entry.get("input_modalities"))
    output_modalities = as_str_list(entry.get("output_modalities"))
    capabilities = derive_subsystems(
        model_type=model_type,
        input_modalities=input_modalities,
        output_modalities=output_modalities,
    )
    capabilities.update(declared)
    display = str(entry.get("display_name", "") or entry.get("name", "") or entry_id)
    return {
        "id": entry_id,
        "name": display,
        "owned_by": str(entry.get("owned_by", "")),
        "capabilities": capabilities,
        "model_type": model_type,
        "input_modalities": input_modalities,
        "output_modalities": output_modalities,
        "languages": parse_languages(entry),
    }


def pick_fallback_language(result: Any, requested_language: Any) -> str | None:
    """A fallback language taken from a Harmony TTS error envelope.

    When a single-speaker model rejects the requested language it returns an error
    envelope whose message lists the supported ones, e.g. ``The model `openvoice_v2` only
    supports the following languages: EN,ZH,ES,FR,JA.`` This reads that list (structured
    API output, not user intent) and returns the best fallback: English if available,
    otherwise the first supported language; ``None`` when no list is present or the
    requested language is already supported.
    """
    if not isinstance(result, dict):
        return None
    if "error" not in result and result.get("object") != "error":
        return None
    message = result.get("message")
    if not isinstance(message, str):
        return None
    marker = "following languages:"
    idx = message.lower().find(marker)
    if idx < 0:
        return None
    tail = message[idx + len(marker) :]
    codes = [
        token.strip().upper()
        for token in tail.replace(".", ",").split(",")
        if token.strip().isalpha()
    ]
    if not codes:
        return None
    requested = str(requested_language or "").upper()
    if requested and requested in codes:
        return None  # already supported: the failure was something else
    return "EN" if "EN" in codes else codes[0]


def decode_audio(result: Any) -> bytes | None:
    """Audio from a Harmony TTS response: ``{"id","model","created","data": "<b64>"}``."""
    if not isinstance(result, dict):
        return None
    if "error" in result or result.get("object") == "error":
        Warn.warning(
            f"[harmony_ai] TTS error envelope: {result.get('message', result)!r}"
        )
        return None
    data = result.get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        return base64.b64decode(data)
    except (ValueError, TypeError) as exc:
        Warn.warning(f"[harmony_ai] failed to decode base64 audio: {exc}")
        return None


# ------------------------------------------------------------------- client


class Harmony:
    """The Harmony endpoint of one request."""

    def __init__(self, endpoint: dict[str, Any], api_key: str) -> None:
        self._base_url = str(endpoint.get("base_url") or "")
        if not self._base_url:
            raise HarmonyError("this endpoint needs a base URL")
        self._api_key = api_key
        extra = endpoint.get("extra_config") or {}
        try:
            self._timeout = float(extra.get("timeout", 300.0))
        except (TypeError, ValueError):
            self._timeout = 300.0

    def _v1(self) -> bool:
        return urlparse(self._base_url).path.rstrip("/").endswith("/v1")

    def _url(self, path: str) -> str:
        parsed = urlparse(self._base_url)
        base_path = parsed.path.rstrip("/")
        joined = (
            f"{base_path}/{path.lstrip('/')}" if base_path else f"/{path.lstrip('/')}"
        )
        return urlunparse(parsed._replace(path=joined))

    def _paths(self, tail: str) -> list[str]:
        return [f"/{tail}"] if self._v1() else [f"/v1/{tail}", f"/{tail}"]

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    # ----------------------------------------------------------------- models

    async def models(self) -> list[dict[str, Any]]:
        """The model listing. Empty when the endpoint never answered (nothing is invented);
        a placeholder when it answered with nothing, so the probe can still ping it."""
        answered = False
        for path in self._paths("models"):
            url = self._url(path)
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.get(url, headers=self._auth)
                # An HTTP response arrived: the endpoint is reachable, however unusable.
                answered = True
                if resp.status_code != 200:
                    Warn.warning(
                        f"[harmony_ai] GET {url} returned HTTP {resp.status_code}"
                    )
                    continue
                data = resp.json()
                if isinstance(data, dict):
                    data = data.get("data", [])
                if not isinstance(data, list):
                    return []
                parsed = [parse_model_entry(item) for item in data]
                found = [m for m in parsed if m["id"]]
                return found or [{"id": "default", "name": "Default Model"}]
            except Exception as exc:  # noqa: BLE001 - the next path may still answer
                Warn.warning(f"[harmony_ai] list_models failed (url={url}): {exc!r}")
        return [{"id": "default", "name": "Default Model"}] if answered else []

    # -------------------------------------------------------------------- tts

    async def tts(
        self, text: str, voice: str | None, kwargs: dict[str, Any]
    ) -> bytes | None:
        """``POST /v1/audio/speech``. The decoded audio, or ``None`` on any failure."""
        model = str(kwargs.get("model", "") or "")
        if not model:
            Warn.warning("[harmony_ai] TTS requires a 'model'; none provided")
            return None
        output_options: dict[str, Any] = {
            "format": str(kwargs.get("format", TTS_FORMAT))
        }
        if kwargs.get("sample_rate") is not None:
            output_options["sample_rate"] = kwargs["sample_rate"]
        payload: dict[str, Any] = {
            "model": model,
            "input": text,
            "mode": str(kwargs.get("mode", TTS_MODE)),
            "output_options": output_options,
        }
        # Only sent when the caller provides them: Harmony rejects language/voice on
        # models that do not advertise them.
        if kwargs.get("language"):
            payload["language"] = str(kwargs["language"])
        if voice:
            payload["voice"] = str(voice)
        if kwargs.get("input_audio"):
            payload["input_audio"] = kwargs["input_audio"]
        if kwargs.get("input_embedding"):
            payload["input_embedding"] = kwargs["input_embedding"]
        if isinstance(kwargs.get("generation_options"), dict):
            payload["generation_options"] = kwargs["generation_options"]
        headers = {**self._auth, "Content-Type": "application/json"}

        for path in self._paths("audio/speech"):
            url = self._url(path)
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code != 200:
                        Warn.warning(
                            f"[harmony_ai] TTS {url} returned {resp.status_code}: {resp.text[:200]}"
                        )
                        continue
                    result = resp.json()
                    audio = decode_audio(result)
                    if audio is not None:
                        return audio
                    # Language-unsupported fallback: retry once with a supported one
                    # (preferring English) before giving up.
                    fallback = pick_fallback_language(result, payload.get("language"))
                    if fallback is not None:
                        Warn.warning(
                            f"[harmony_ai] TTS language {payload.get('language')!r} "
                            f"unsupported by {model!r}; retrying with {fallback!r}"
                        )
                        retry = await client.post(
                            url, json={**payload, "language": fallback}, headers=headers
                        )
                        if retry.status_code == 200:
                            audio = decode_audio(retry.json())
                            if audio is not None:
                                return audio
                        else:
                            Warn.warning(
                                f"[harmony_ai] TTS retry {url} returned {retry.status_code}: "
                                f"{retry.text[:200]}"
                            )
            except Exception as exc:  # noqa: BLE001
                Warn.warning(
                    f"[harmony_ai] TTS request failed ({url}): {type(exc).__name__}: {exc}"
                )
        return None

    # -------------------------------------------------------------------- stt

    async def stt(self, audio: bytes, kwargs: dict[str, Any]) -> str | None:
        """``POST /v1/audio/transcriptions`` (JSON, base64 ``input_audio``)."""
        model = str(kwargs.get("model", "") or "")
        if not model:
            Warn.warning("[harmony_ai] STT requires a 'model'; none provided")
            return None
        payload: dict[str, Any] = {
            "model": model,
            "input_audio": base64.b64encode(audio).decode("ascii"),
        }
        if kwargs.get("get_language") is not None:
            payload["get_language"] = bool(kwargs["get_language"])
        if kwargs.get("get_timestamps") is not None:
            payload["get_timestamps"] = bool(kwargs["get_timestamps"])
        headers = {**self._auth, "Content-Type": "application/json"}

        for path in self._paths("audio/transcriptions"):
            url = self._url(path)
            try:
                async with httpx.AsyncClient(timeout=300.0) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    Warn.warning(
                        f"[harmony_ai] STT {url} returned {resp.status_code}: {resp.text[:200]}"
                    )
                    continue
                result = resp.json()
                if isinstance(result, dict) and isinstance(result.get("text"), str):
                    return result["text"]
            except Exception as exc:  # noqa: BLE001
                Warn.warning(f"[harmony_ai] STT request failed ({url}): {exc}")
        return None
