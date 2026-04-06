# core/external_endpoints/adapters/base.py
"""Abstract base class for external endpoint protocol adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class ModelInfo:
    """Minimal model descriptor returned by an adapter."""

    id: str
    name: str
    owned_by: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)


@dataclass
class ChatResponse:
    """Normalised response from a chat completion call."""

    content: str
    model: str = ""
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)


class BaseProtocolAdapter(ABC):
    """Common interface every external endpoint adapter must implement."""

    # ------------------------------------------------------------------
    # Chat / LLM
    # ------------------------------------------------------------------

    @abstractmethod
    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatResponse:
        """Send a chat completion request.

        Args:
            messages: OpenAI-style message list (role/content dicts).
            model:    Model name override.
            stream:   Whether to stream the response.
            **kwargs: Adapter-specific extra parameters.

        Returns:
            :class:`ChatResponse` with the assistant content.
        """

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        """Return the list of models available on this endpoint."""

    # ------------------------------------------------------------------
    # TTS (Vox)
    # ------------------------------------------------------------------

    async def generate_tts(
        self,
        text: str,
        voice: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        """Convert text to audio bytes.

        Returns ``None`` if TTS is not supported by this adapter.
        """
        return None

    # ------------------------------------------------------------------
    # STT (Auris)
    # ------------------------------------------------------------------

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        mime_type: str | None = None,
        **kwargs: Any,
    ) -> str | None:
        """Transcribe audio bytes to text.

        Returns ``None`` if STT is not supported by this adapter.
        """
        return None

    # ------------------------------------------------------------------
    # Probe / health
    # ------------------------------------------------------------------

    @abstractmethod
    async def probe_capabilities(self) -> dict[str, bool]:
        """Auto-detect which SyntH subsystems this endpoint supports.

        Returns a dict mapping subsystem names to ``True`` / ``False``:
        ``{"cortex": True, "vox": False, "auris": False, "live": False}``.
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """Return ``True`` if the endpoint is reachable and responsive."""

    async def ping_test(
        self,
        model: str | None = None,
        timeout: float = 15.0,
    ) -> tuple[bool, str]:
        """Send a minimal chat 'ping' to verify cortex connectivity.

        Returns ``(True, response_text)`` on success, ``(False, error_str)``
        on failure.  The default implementation returns ``(False, ...)``;
        concrete adapters should override this when possible.
        """
        return False, "ping_test not implemented for this adapter"

    # ------------------------------------------------------------------
    # Optional streaming (Cortex)
    # ------------------------------------------------------------------

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Yield response text chunks as they arrive (optional).

        The default implementation falls back to a single non-streaming call.
        """
        response = await self.chat_completion(messages, model=model, **kwargs)
        yield response.content
