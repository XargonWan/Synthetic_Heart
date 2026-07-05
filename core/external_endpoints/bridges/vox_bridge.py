# core/external_endpoints/bridges/vox_bridge.py
"""Vox (TTS) bridge for external endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugins.vox_base import VoxEngineBase

if TYPE_CHECKING:
    from core.external_endpoints.adapters.base import BaseProtocolAdapter
    from core.external_endpoints.models import ExternalEndpoint


class ExternalVoxEngine(VoxEngineBase):
    """VoxEngineBase implementation backed by an external endpoint adapter."""

    def __init__(
        self,
        endpoint: "ExternalEndpoint",
        adapter: "BaseProtocolAdapter | None",
    ) -> None:
        self._endpoint = endpoint
        self._adapter = adapter
        if self._adapter is not None:
            self._adapter._engine_label = endpoint.name or "vox_bridge"
        self.display_name = f"{endpoint.display_label or endpoint.name} (TTS)"

    @property
    def output_format(self) -> str:
        extra = self._endpoint.extra_config or {}
        return str(extra.get("tts_output_format", extra.get("output_format", "wav")))

    @property
    def sample_rate(self) -> int:
        extra = self._endpoint.extra_config or {}
        try:
            return int(extra.get("tts_sample_rate", extra.get("sample_rate", 22050)))
        except Exception:
            return 22050

    @property
    def channels(self) -> int:
        extra = self._endpoint.extra_config or {}
        try:
            return int(extra.get("tts_channels", extra.get("channels", 1)))
        except Exception:
            return 1

    def generate_tts(
        self,
        text: str,
        emotion: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        """Synchronous wrapper – runs the async adapter call in the event loop."""
        import asyncio

        if self._adapter is None:
            return None

        extra = self._endpoint.extra_config or {}
        voice = extra.get("tts_voice") or kwargs.get("voice")

        # Resolve the TTS model for this endpoint.  Multi-modal endpoints (e.g.
        # Harmony) use a dedicated ``tts_model`` in ``extra_config`` because the
        # endpoint's ``default_model`` is reserved for the cortex/text engine and
        # is not a valid text-to-speech model.
        if "model" not in kwargs:
            tts_model = extra.get("tts_model") or self._endpoint.default_model
            if tts_model:
                kwargs["model"] = tts_model

        # Some single-speaker TTS models (e.g. KittenTTS) require an explicit
        # language.  Allow it to be configured per-endpoint.
        if "language" not in kwargs:
            tts_language = extra.get("tts_language")
            if tts_language:
                kwargs["language"] = tts_language

        coro = self._adapter.generate_tts(text, voice=voice, **kwargs)
        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is not None:
                # If called from an already-running loop (e.g. FastAPI), schedule
                # it as a task and block synchronously via a Future.
                import concurrent.futures

                future: concurrent.futures.Future[bytes | None] = (
                    concurrent.futures.Future()
                )

                async def _run() -> None:
                    try:
                        result = await coro
                        future.set_result(result)
                    except Exception as exc:
                        future.set_exception(exc)

                asyncio.ensure_future(_run())
                return future.result(timeout=60)
            else:
                # No running loop in this thread (e.g. called via
                # ``asyncio.to_thread``): run the coroutine to completion here.
                return asyncio.run(coro)
        except Exception:
            return None


# Required by VoxRegistry::load_engine() when loading via module path
ENGINE_CLASS = ExternalVoxEngine
