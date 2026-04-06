# core/external_endpoints/adapters/gemini_adapter.py
"""Adapter for Google Gemini endpoints via the google-genai SDK."""

from __future__ import annotations

from typing import Any, AsyncIterator

from core.logging_utils import log_warning

from core.external_endpoints.adapters.base import (
    BaseProtocolAdapter,
    ChatResponse,
    ModelInfo,
)


def _messages_to_gemini(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Split OpenAI-style messages into (system_instruction, gemini_contents)."""
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
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append({"text": item.get("text", "")})
            contents.append({"role": gemini_role, "parts": parts})
    return "\n".join(system_parts), contents


class GeminiAdapter(BaseProtocolAdapter):
    """Adapter using the ``google-genai`` SDK for Google Gemini services."""

    DEFAULT_MODEL = "gemini-2.0-flash"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _get_client(self) -> Any:
        try:
            from google import genai

            return genai.Client(api_key=self._api_key)
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

        try:
            from google.genai import types

            config_kwargs: dict[str, Any] = {}
            if system_instruction:
                config_kwargs["system_instruction"] = system_instruction

            def _sync_generate() -> Any:
                return client.models.generate_content(
                    model=request_model,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_kwargs)
                    if config_kwargs
                    else None,
                )

            response = await asyncio.get_event_loop().run_in_executor(
                None, _sync_generate
            )
            content_text = ""
            if response.text:
                content_text = response.text
            elif response.candidates:
                for cand in response.candidates:
                    if cand.content and cand.content.parts:
                        content_text = "".join(
                            p.text for p in cand.content.parts if hasattr(p, "text")
                        )
                        break

            return ChatResponse(
                content=content_text,
                model=request_model,
                finish_reason="stop",
            )
        except Exception:
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

        try:
            from google.genai import types

            def _sync_transcribe() -> str:
                response = client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=[
                        types.Part.from_bytes(
                            data=audio_bytes, mime_type=effective_mime
                        ),
                        "Transcribe the audio accurately.",
                    ],
                )
                return response.text or ""

            return await asyncio.get_event_loop().run_in_executor(
                None, _sync_transcribe
            )
        except Exception as exc:
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

            return await asyncio.get_event_loop().run_in_executor(
                None, _sync_describe
            )
        except Exception as exc:
            log_warning(f"[gemini_adapter] describe_image failed: {exc}")
            return None
