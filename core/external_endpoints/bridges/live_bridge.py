# core/external_endpoints/bridges/live_bridge.py
"""Live (bidirectional-streaming) bridge for external endpoints.

Currently a stub that logs a warning.  Full bidirectional streaming support
via external endpoints requires WebSocket/real-time protocol negotiation which
varies per provider; a proper implementation is planned for a future iteration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator

from plugins.live_base import LiveEngineBase, LiveEvent
from core.logging_utils import log_warning

if TYPE_CHECKING:
    from core.external_endpoints.adapters.base import BaseProtocolAdapter
    from core.external_endpoints.models import ExternalEndpoint


class ExternalLiveEngine(LiveEngineBase):
    """LiveEngineBase stub for external endpoints.

    Logs a warning and does nothing.  Enables endpoints to be registered in
    the Live registry so the slot is reserved, and allows future
    implementations to replace this class without changing the registry layer.
    """

    @property
    def supports_input(self) -> bool:
        return False

    @property
    def supports_output(self) -> bool:
        return False

    def __init__(
        self,
        endpoint: "ExternalEndpoint",
        adapter: "BaseProtocolAdapter",
    ) -> None:
        self._endpoint = endpoint
        self._adapter = adapter
        self.display_name = f"{endpoint.display_label or endpoint.name} (Live)"

    async def open_session(self, session_id: str, **kwargs: Any) -> None:
        log_warning(
            f"[live_bridge:{self._endpoint.name}] Live sessions via external endpoints "
            "are not yet supported."
        )

    async def close_session(self, session_id: str) -> None:
        pass

    async def send_audio(
        self,
        session_id: str,
        chunk: bytes,
        sample_rate: int = 16000,
    ) -> None:
        pass

    async def receive_events(
        self,
        session_id: str,
    ) -> AsyncIterator[LiveEvent]:
        return
        yield  # make this an async generator

    async def send_text(self, session_id: str, text: str) -> None:
        pass


# Required by LiveRegistry::load_engine() when loading via module path
ENGINE_CLASS = ExternalLiveEngine
