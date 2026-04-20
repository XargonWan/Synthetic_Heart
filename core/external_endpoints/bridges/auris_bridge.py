# core/external_endpoints/bridges/auris_bridge.py
"""Auris (STT) bridge for external endpoints."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from plugins.auris_base import AurisEngineBase, AurisTranscriptResult

if TYPE_CHECKING:
    from core.external_endpoints.adapters.base import BaseProtocolAdapter
    from core.external_endpoints.models import ExternalEndpoint


class ExternalAurisEngine(AurisEngineBase):
    """AurisEngineBase implementation backed by an external endpoint adapter."""

    def __init__(
        self,
        endpoint: "ExternalEndpoint",
        adapter: "BaseProtocolAdapter",
    ) -> None:
        self._endpoint = endpoint
        self._adapter = adapter
        self.display_name = f"{endpoint.display_label or endpoint.name} (STT)"

    def transcribe(
        self,
        file_path: str,
        mime_type: str | None = None,
    ) -> AurisTranscriptResult | None:
        """Read *file_path* and forward audio bytes to the external adapter."""
        import asyncio

        if not os.path.exists(file_path):
            return None

        with open(file_path, "rb") as fh:
            audio_bytes = fh.read()

        if not audio_bytes:
            return None

        coro = self._adapter.transcribe_audio(audio_bytes, mime_type=mime_type)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                future: concurrent.futures.Future[str | None] = (
                    concurrent.futures.Future()
                )

                async def _run() -> None:
                    try:
                        result = await coro
                        future.set_result(result)
                    except Exception as exc:
                        future.set_exception(exc)

                asyncio.ensure_future(_run())
                text = future.result(timeout=120)
            else:
                text = loop.run_until_complete(coro)
        except Exception:
            return None

        if text is None:
            return None

        return AurisTranscriptResult(text=text, language=None)


# Required by AurisRegistry::load_engine() when loading via module path
ENGINE_CLASS = ExternalAurisEngine
