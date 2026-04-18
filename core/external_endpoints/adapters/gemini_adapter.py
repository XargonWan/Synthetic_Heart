# core/external_endpoints/adapters/gemini_adapter.py
"""Adapter for Google Gemini endpoints via the google-genai SDK."""

from __future__ import annotations

import time as _time
from typing import Any, AsyncIterator

from core.cortex_api_logger import (
    log_cortex_request,
    log_cortex_response,
)
from core.genai_client_utils import harden_genai_client_for_async_close
from core.logging_utils import log_warning

from core.external_endpoints.adapters.base import (
    BaseProtocolAdapter,
    ChatResponse,
    ModelInfo,
)


def _messages_to_gemini(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Split OpenAI-style messages into (system_instruction, gemini_contents).

    Handles multipart content lists containing ``text``, ``inline_data``
    (native SyntH multimodal), and ``image_url`` (OpenAI-compat data-URIs).
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            continue
        if role == "assistant":
            gemini_role = "model"
        else:
            gemini_role = "user"
        if isinstance(content, str):
            contents.append({"role": gemini_role, "parts": [{"text": content}]})
        elif isinstance(content, list):
            parts: list[dict[str, Any]] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type", "")
                if item_type == "text":
                    parts.append({"text": item.get("text", "")})
                elif item_type == "inline_data":
                    # Native SyntH multimodal — pass through to Gemini
                    inline = item.get("inline_data", {})
                    if inline.get("mime_type") and inline.get("data"):
                        parts.append({"inline_data": inline})
                elif item_type == "image_url":
                    # OpenAI image_url compat — convert data-URIs to inline_data
                    url = (item.get("image_url") or {}).get("url", "")
                    if url.startswith("data:"):
                        try:
                            header, b64data = url.split(",", 1)
                            mime = header.split(":")[1].split(";")[0]
                            parts.append(
                                {
                                    "inline_data": {
                                        "mime_type": mime,
                                        "data": b64data,
                                    }
                                }
                            )
                        except (ValueError, IndexError):
                            pass
            if parts:
                contents.append({"role": gemini_role, "parts": parts})
    return "\n".join(system_parts), contents


class GeminiAdapter(BaseProtocolAdapter):
    """Adapter using the ``google-genai`` SDK for Google Gemini services."""

    DEFAULT_MODEL = "gemini-3-flash-preview"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _get_client(self) -> Any:
        try:
            from google import genai

            return harden_genai_client_for_async_close(
                genai.Client(api_key=self._api_key)
            )
        except ImportError as exc:
            raise RuntimeError(
                "[gemini_adapter] The 'google-genai' package is required."
            ) from exc

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatResponse:
        import asyncio

        client = self._get_client()
        request_model = model or self.DEFAULT_MODEL
        system_instruction, contents = _messages_to_gemini(messages)
        engine_tag = f"gemini:{self._engine_label or 'default'}"
        _req_start = _time.monotonic()

        try:
            from google.genai import types

            config_kwargs: dict[str, Any] = {}
            if system_instruction:
                config_kwargs["system_instruction"] = system_instruction
            # Enforce JSON output — SyntH action parser requires structured JSON.
            config_kwargs["response_mime_type"] = "application/json"

            # ── Safety settings — disable all content filters ─────────
            # The persona context can contain extreme content that trips
            # Gemini's default safety filters even on innocuous queries.
            # Set every harm category to OFF (filter disabled).
            config_kwargs["safety_settings"] = [
                types.SafetySetting(
                    category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                    threshold=types.HarmBlockThreshold.OFF,
                ),
                types.SafetySetting(
                    category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                    threshold=types.HarmBlockThreshold.OFF,
                ),
                types.SafetySetting(
                    category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                    threshold=types.HarmBlockThreshold.OFF,
                ),
                types.SafetySetting(
                    category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                    threshold=types.HarmBlockThreshold.OFF,
                ),
            ]

            log_cortex_request(
                engine_tag,
                model=request_model,
                payload={
                    "system_instruction": system_instruction or None,
                    "contents": contents,
                    "response_mime_type": config_kwargs.get("response_mime_type"),
                },
            )

            def _sync_generate() -> Any:
                return client.models.generate_content(
                    model=request_model,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_kwargs)
                    if config_kwargs
                    else None,
                )

            request_timeout_s = float(kwargs.get("timeout", 120) or 120)
            response = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _sync_generate),
                timeout=request_timeout_s,
            )
            content_text = ""
            finish = "stop"

            # ── Safety / block detection ──────────────────────────────
            # Gemini may return an empty response when the prompt or
            # output is blocked by safety filters.  Detect this early
            # so the caller gets a clear signal instead of silent "".
            prompt_blocked = False
            block_reason: str | None = None
            try:
                pf = getattr(response, "prompt_feedback", None)
                if pf:
                    br = getattr(pf, "block_reason", None)
                    if br and str(br) not in ("", "BLOCK_REASON_UNSPECIFIED"):
                        prompt_blocked = True
                        block_reason = str(br)
                        log_warning(
                            f"[gemini_adapter] Prompt blocked by safety filter: "
                            f"block_reason={block_reason}"
                        )
            except Exception:
                pass

            if not prompt_blocked:
                # Try .text first (raises on per-candidate safety blocks)
                try:
                    if response.text:
                        content_text = response.text
                except ValueError as ve:
                    # SDK raises ValueError when candidate finish_reason is
                    # SAFETY / RECITATION / etc.
                    log_warning(
                        f"[gemini_adapter] response.text raised ValueError "
                        f"(likely safety block): {ve}"
                    )
                    finish = "safety"

                # Fallback: iterate candidates manually
                if not content_text and not prompt_blocked:
                    try:
                        if response.candidates:
                            cand = response.candidates[0]
                            fr = getattr(cand, "finish_reason", None)
                            if fr:
                                finish = str(fr).lower()
                            if cand.content and cand.content.parts:
                                content_text = "".join(
                                    p.text
                                    for p in cand.content.parts
                                    if hasattr(p, "text") and p.text
                                )
                    except Exception:
                        pass

            if not content_text and (prompt_blocked or finish == "safety"):
                log_warning(
                    f"[gemini_adapter] Empty response — likely safety-filtered "
                    f"(finish={finish}, block_reason={block_reason})"
                )

            _elapsed = (_time.monotonic() - _req_start) * 1000
            logged_body: Any = content_text
            if not content_text:
                logged_body = {
                    "empty_response": True,
                    "finish_reason": finish,
                    "block_reason": block_reason,
                }
            log_cortex_response(
                engine_tag,
                model=request_model,
                status=200,
                body=logged_body,
                elapsed_ms=_elapsed,
            )

            return ChatResponse(
                content=content_text,
                model=request_model,
                finish_reason=finish,
            )
        except asyncio.CancelledError:
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model=request_model,
                status=499,
                error="request cancelled",
                elapsed_ms=_elapsed,
            )
            raise
        except asyncio.TimeoutError:
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model=request_model,
                status=504,
                error="request timed out",
                elapsed_ms=_elapsed,
            )
            raise
        except Exception as exc:
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model=request_model,
                status=500,
                error=str(exc),
                elapsed_ms=_elapsed,
            )
            raise

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        # Fallback to non-streaming for simplicity; Gemini streaming is complex
        response = await self.chat_completion(messages, model=model, **kwargs)
        yield response.content

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    async def list_models(self) -> list[ModelInfo]:
        import asyncio

        client = self._get_client()
        try:

            def _sync_list() -> Any:
                return list(client.models.list())

            models = await asyncio.get_event_loop().run_in_executor(None, _sync_list)
            result = []
            for m in models:
                mid = getattr(m, "name", "") or ""
                # Strip 'models/' prefix that the SDK sometimes returns
                mid = mid.removeprefix("models/")
                if mid:
                    result.append(ModelInfo(id=mid, name=mid, owned_by="google"))
            return result
        except Exception as exc:
            log_warning(f"[gemini_adapter] list_models failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # Probe / health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        try:
            models = await self.list_models()
            return len(models) > 0
        except Exception:
            return False

    async def ping_test(
        self,
        model: str | None = None,
        timeout: float = 15.0,
    ) -> tuple[bool, str]:
        """Send a minimal generate_content call to verify Gemini API connectivity."""
        import asyncio

        client = self._get_client()
        request_model = model or self.DEFAULT_MODEL
        try:

            def _sync_ping() -> str:
                response = client.models.generate_content(
                    model=request_model,
                    contents=[{"role": "user", "parts": [{"text": "ping"}]}],
                )
                try:
                    return response.text or "ok"
                except Exception:
                    return "ok"

            reply = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _sync_ping),
                timeout=timeout,
            )
            return True, reply
        except asyncio.TimeoutError:
            return False, f"ping timed out after {timeout}s"
        except Exception as exc:
            return False, repr(exc)

    async def probe_capabilities(self) -> dict[str, bool]:
        caps: dict[str, bool] = {
            "cortex": False,
            "vox": False,
            "auris": True,  # Gemini supports audio transcription
            "live": False,
            "vision": False,
        }
        try:
            models = await self.list_models()
            if models:
                caps["cortex"] = True
            for m in models:
                mid = m.id.lower()
                if "live" in mid:
                    caps["live"] = True
                if any(kw in mid for kw in ("vision", "vl", "flash", "pro")):
                    caps["vision"] = True
        except Exception:
            pass
        return caps

    # ------------------------------------------------------------------
    # TTS  (not supported by standard Gemini API)
    # ------------------------------------------------------------------

    async def generate_tts(
        self,
        text: str,
        voice: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        return None  # Gemini standard API does not offer TTS

    # ------------------------------------------------------------------
    # STT via Gemini multimodal
    # ------------------------------------------------------------------

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        mime_type: str | None = None,
        **kwargs: Any,
    ) -> str | None:
        import asyncio

        client = self._get_client()
        effective_mime = mime_type or "audio/wav"
        engine_tag = f"gemini:{self._engine_label or 'default'}"
        _req_start = _time.monotonic()

        log_cortex_request(
            engine_tag,
            model="gemini-3-flash-preview",
            payload={
                "task": "transcribe_audio",
                "mime_type": effective_mime,
                "audio_size": f"{len(audio_bytes)} bytes",
            },
        )

        try:
            from google.genai import types

            def _sync_transcribe() -> str:
                response = client.models.generate_content(
                    model="gemini-3-flash-preview",
                    contents=[
                        types.Part.from_bytes(
                            data=audio_bytes, mime_type=effective_mime
                        ),
                        "Transcribe the audio accurately.",
                    ],
                )
                return response.text or ""

            result = await asyncio.get_event_loop().run_in_executor(
                None, _sync_transcribe
            )
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model="gemini-3-flash-preview",
                body=result,
                elapsed_ms=_elapsed,
            )
            return result
        except Exception as exc:
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model="gemini-3-flash-preview",
                error=str(exc),
                elapsed_ms=_elapsed,
            )
            log_warning(f"[gemini_adapter] transcribe_audio failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Vision (Iris) – Gemini inline_data format
    # ------------------------------------------------------------------

    async def describe_image(
        self,
        image_bytes: bytes,
        mime_type: str | None = None,
        prompt: str | None = None,
        **kwargs: Any,
    ) -> str | None:
        """Describe *image_bytes* using Gemini multimodal inline_data.

        Returns ``None`` if the request fails.
        """
        import asyncio

        client = self._get_client()
        effective_mime = mime_type or "image/jpeg"
        effective_prompt = prompt or "Describe this image in detail."
        engine_tag = f"gemini:{self._engine_label or 'default'}"
        _req_start = _time.monotonic()

        log_cortex_request(
            engine_tag,
            model="gemini-2.0-flash",
            payload={
                "task": "describe_image",
                "mime_type": effective_mime,
                "image_size": f"{len(image_bytes)} bytes",
                "prompt": effective_prompt,
            },
        )

        try:
            from google.genai import types

            def _sync_describe() -> str:
                response = client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=[
                        types.Part.from_bytes(
                            data=image_bytes, mime_type=effective_mime
                        ),
                        effective_prompt,
                    ],
                )
                return response.text or ""

            result = await asyncio.get_event_loop().run_in_executor(
                None, _sync_describe
            )
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model="gemini-2.0-flash",
                body=result,
                elapsed_ms=_elapsed,
            )
            return result
        except Exception as exc:
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model="gemini-2.0-flash",
                error=str(exc),
                elapsed_ms=_elapsed,
            )
            log_warning(f"[gemini_adapter] describe_image failed: {exc}")
            return None
