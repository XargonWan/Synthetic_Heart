# core/external_endpoints/adapters/harmony_ai_adapter.py
"""Adapter for the Harmony AI Cloud API (project-harmony.ai).

Harmony AI exposes an OpenAI-compatible surface for **chat** and
**embeddings**, but its **audio** endpoints are deliberately *not*
OpenAI-shaped.  Keeping :class:`OpenAICompatAdapter` strictly standard means
all of Harmony's proprietary behaviour lives here.

Key deviations from the plain OpenAI contract (per the Harmony Cloud API
guide):

* ``GET /v1/models`` is public and returns rich descriptors OpenAI never
  emits: ``model_type`` (``llm`` / ``tts`` / ``stt`` / ``embeddings`` / ...),
  ``input_modalities`` / ``output_modalities`` arrays, and a
  ``speech_options`` object (``languages`` / ``voices`` / cloning support).
  The canonical id field is ``model_id`` (not ``id``).
* ``POST /v1/audio/speech`` (TTS) takes a **JSON** body — not multipart —
  with a required ``mode`` field (``single_speaker_tts`` / ``voice_cloning``),
  an ``output_options.format`` selector, and returns the audio **base64 in a
  JSON** ``data`` field (not raw bytes).  ``language`` / ``voice`` are only
  valid for models that advertise them (single-speaker); cloning models take
  ``input_audio`` / ``input_embedding`` instead.
* ``POST /v1/audio/transcriptions`` (STT) also takes **JSON** with the audio
  base64-encoded in ``input_audio`` (not a multipart file) and returns the
  transcript in a JSON ``text`` field.
* Errors are returned with real HTTP status codes and an
  ``{"error": ..., "message": ...}`` body.

This adapter subclasses the OpenAI adapter (chat / embeddings / vision stay
standard) and overrides only the model-parsing and audio paths.
"""

from __future__ import annotations

import base64
from typing import Any

from core.logging_utils import log_debug, log_warning

from core.external_endpoints.adapters.base import ModelInfo
from core.external_endpoints.adapters.openai_compat import OpenAICompatAdapter

# Default synthesis mode when the caller does not specify one. KittenTTS and
# OpenVoice support single-speaker; cloning-only models (Chatterbox, etc.)
# require the caller to pass mode="voice_cloning" plus a reference.
_HARMONY_TTS_MODE = "single_speaker_tts"
# Default output container requested via Harmony's ``output_options.format``.
_HARMONY_TTS_FORMAT = "mp3"


class HarmonyAIAdapter(OpenAICompatAdapter):
    """OpenAI-based adapter with Harmony AI's proprietary audio extensions."""

    # ------------------------------------------------------------------
    # Model metadata parsing (Harmony custom descriptors)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_languages(entry: dict[str, Any]) -> list[dict[str, str]]:
        """Extract supported languages from a Harmony model entry.

        Handles ``speech_options.languages`` = ``[{"code","name"}, ...]`` and a
        flat ``languages`` list of codes. Deterministic — no keyword matching.
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

    def _derive_model_subsystems(
        self,
        *,
        model_type: str,
        input_modalities: list[str],
        output_modalities: list[str],
    ) -> dict[str, bool]:
        """Map Harmony's structured model metadata onto SyntH subsystems.

        Purely structural (model_type + modality arrays), never keyword-based,
        so it works across languages and naming conventions.
        """
        mt = (model_type or "").lower()
        in_mods = set(input_modalities)
        out_mods = set(output_modalities)
        caps: dict[str, bool] = {}

        # cortex: an LLM (or any text->text generator)
        if mt == "llm" or ("text" in in_mods and "text" in out_mods):
            caps["cortex"] = True
        # vox (TTS): produces audio from text
        if mt == "tts" or ("text" in in_mods and "audio" in out_mods):
            caps["vox"] = True
        # auris (STT): audio -> text
        if mt == "stt" or ("audio" in in_mods and "text" in out_mods):
            caps["auris"] = True
        # vision: consumes image or video
        if "image" in in_mods or "video" in in_mods:
            caps["vision"] = True
        return caps

    def _parse_model_entry(self, entry: Any) -> ModelInfo:
        """Parse a ``/v1/models`` entry, honouring Harmony's rich descriptors.

        Falls back to the standard OpenAI parsing for SDK model objects.
        """
        if isinstance(entry, dict):
            # Harmony's canonical id field is ``model_id``; fall back to ``id``.
            entry_id = str(entry.get("model_id", "") or entry.get("id", "") or "")
            declared_caps = self._normalize_capabilities(entry.get("capabilities", {}))
            model_type = str(entry.get("model_type", "") or "")
            input_modalities = self._as_str_list(entry.get("input_modalities"))
            output_modalities = self._as_str_list(entry.get("output_modalities"))
            languages = self._parse_languages(entry)
            # Merge structurally-derived caps with any explicitly-declared ones.
            capabilities = self._derive_model_subsystems(
                model_type=model_type,
                input_modalities=input_modalities,
                output_modalities=output_modalities,
            )
            capabilities.update(declared_caps)
            display = str(
                entry.get("display_name", "") or entry.get("name", "") or entry_id
            )
            return ModelInfo(
                id=entry_id,
                name=display,
                owned_by=str(entry.get("owned_by", "")),
                capabilities=capabilities,
                model_type=model_type,
                input_modalities=input_modalities,
                output_modalities=output_modalities,
                languages=languages,
            )
        return super()._parse_model_entry(entry)

    # ------------------------------------------------------------------
    # TTS (Harmony JSON body: mode + optional language/voice + cloning refs)
    # ------------------------------------------------------------------

    async def generate_tts(
        self,
        text: str,
        voice: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        """Generate TTS via Harmony's ``POST /v1/audio/speech`` endpoint.

        Harmony's TTS is JSON-only and *not* OpenAI-shaped. The request body
        carries a required ``mode`` (``single_speaker_tts`` / ``voice_cloning``)
        and returns the audio **base64-encoded** in the JSON ``data`` field.
        ``language`` / ``voice`` are only sent when supplied (single-speaker
        models); cloning models instead take ``input_audio`` /
        ``input_embedding`` references, which are forwarded from ``kwargs``.

        Returns the decoded audio bytes, or ``None`` on any failure.
        """
        import aiohttp

        model = str(kwargs.get("model", "") or "")
        if not model:
            log_warning("[harmony_ai] TTS requires a 'model'; none provided")
            return None

        output_options: dict[str, Any] = {
            "format": str(kwargs.get("format", _HARMONY_TTS_FORMAT)),
        }
        sample_rate = kwargs.get("sample_rate")
        if sample_rate is not None:
            output_options["sample_rate"] = sample_rate

        payload: dict[str, Any] = {
            "model": model,
            "input": text,
            "mode": str(kwargs.get("mode", _HARMONY_TTS_MODE)),
            "output_options": output_options,
        }
        # Conditional fields — only sent when the caller provides them, since
        # Harmony rejects language/voice on models that don't advertise them.
        language = kwargs.get("language")
        if language:
            payload["language"] = str(language)
        if voice:
            payload["voice"] = str(voice)
        input_audio = kwargs.get("input_audio")
        if input_audio:
            payload["input_audio"] = input_audio
        input_embedding = kwargs.get("input_embedding")
        if input_embedding:
            payload["input_embedding"] = input_embedding
        generation_options = kwargs.get("generation_options")
        if isinstance(generation_options, dict):
            payload["generation_options"] = generation_options

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        for path in self._http_tts_paths():
            url = self._resolve_http_url(path)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as resp:
                        if resp.status != 200:
                            detail = await resp.text()
                            log_warning(
                                f"[harmony_ai] TTS {url} returned {resp.status}: "
                                f"{detail[:200]}"
                            )
                            continue
                        result = await resp.json()
                        audio = self._decode_harmony_audio(result)
                        if audio is not None:
                            return audio
                        # Language-unsupported fallback: if the model rejected
                        # the requested language, retry once with a supported
                        # one (preferring English) before giving up.
                        fallback_language = self._pick_fallback_language(
                            result, payload.get("language")
                        )
                        if fallback_language is not None:
                            log_warning(
                                f"[harmony_ai] TTS language "
                                f"{payload.get('language')!r} unsupported by "
                                f"{model!r}; retrying with "
                                f"{fallback_language!r}"
                            )
                            retry_payload = dict(payload)
                            retry_payload["language"] = fallback_language
                            async with session.post(
                                url,
                                json=retry_payload,
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=120),
                            ) as retry_resp:
                                if retry_resp.status == 200:
                                    retry_result = await retry_resp.json()
                                    audio = self._decode_harmony_audio(retry_result)
                                    if audio is not None:
                                        return audio
                                else:
                                    detail = await retry_resp.text()
                                    log_warning(
                                        f"[harmony_ai] TTS retry {url} returned "
                                        f"{retry_resp.status}: {detail[:200]}"
                                    )
            except Exception as exc:
                log_warning(
                    f"[harmony_ai] TTS request failed ({url}): "
                    f"{type(exc).__name__}: {exc}"
                )
        return None

    @staticmethod
    def _pick_fallback_language(result: Any, requested_language: Any) -> str | None:
        """Extract a fallback language from a Harmony TTS error envelope.

        When a single-speaker model rejects the requested language it returns
        an error envelope whose message lists the supported languages, e.g.
        ``The model `openvoice_v2` only supports the following languages:
        EN,ZH,ES,FR,JA.``  This parses that list (structured API output, not
        user intent) and returns the best fallback — English if available,
        otherwise the first supported language — or ``None`` when no list is
        present or the requested language is already supported.
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
        # Keep only the language-code segment (letters, commas, spaces).
        codes: list[str] = []
        for token in tail.replace(".", ",").split(","):
            code = token.strip().upper()
            if code.isalpha():
                codes.append(code)
        if not codes:
            return None
        requested = str(requested_language or "").upper()
        if requested and requested in codes:
            # Already supported — the failure was something else.
            return None
        if "EN" in codes:
            return "EN"
        return codes[0]

    # ------------------------------------------------------------------
    # STT (Harmony JSON body: base64 input_audio)
    # ------------------------------------------------------------------

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        mime_type: str | None = None,
        **kwargs: Any,
    ) -> str | None:
        """Transcribe audio via Harmony's ``POST /v1/audio/transcriptions``.

        Harmony's STT is JSON-only: the audio is base64-encoded in the
        ``input_audio`` field (not a multipart file), and the transcript is
        returned in the JSON ``text`` field. Returns the text or ``None``.
        """
        import aiohttp

        model = str(kwargs.get("model", "") or "")
        if not model:
            log_warning("[harmony_ai] STT requires a 'model'; none provided")
            return None

        payload: dict[str, Any] = {
            "model": model,
            "input_audio": base64.b64encode(audio_bytes).decode("ascii"),
        }
        get_language = kwargs.get("get_language")
        if get_language is not None:
            payload["get_language"] = bool(get_language)
        get_timestamps = kwargs.get("get_timestamps")
        if get_timestamps is not None:
            payload["get_timestamps"] = bool(get_timestamps)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        for path in self._http_stt_paths():
            url = self._resolve_http_url(path)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=300),
                    ) as resp:
                        if resp.status != 200:
                            detail = await resp.text()
                            log_warning(
                                f"[harmony_ai] STT {url} returned {resp.status}: "
                                f"{detail[:200]}"
                            )
                            continue
                        result = await resp.json()
                        if isinstance(result, dict):
                            text = result.get("text")
                            if isinstance(text, str):
                                return text
            except Exception as exc:
                log_debug(f"[harmony_ai] STT request failed ({url}): {exc}")
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_harmony_audio(result: Any) -> bytes | None:
        """Decode the base64 audio from a Harmony TTS JSON response.

        A successful response is ``{"id", "model", "created", "data": "<b64>"}``.
        An error envelope (``{"error", "message"}``) yields ``None``.
        """
        if not isinstance(result, dict):
            return None
        if "error" in result or result.get("object") == "error":
            log_warning(
                f"[harmony_ai] TTS error envelope: {result.get('message', result)!r}"
            )
            return None
        data = result.get("data")
        if not isinstance(data, str) or not data:
            return None
        try:
            return base64.b64decode(data)
        except (ValueError, TypeError) as exc:
            log_warning(f"[harmony_ai] failed to decode base64 audio: {exc}")
            return None
