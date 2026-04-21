# core/external_endpoints/bridges/iris_bridge.py
"""Iris (vision) bridge for external endpoints."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from plugins.iris_base import IrisEngineBase, IrisResult

if TYPE_CHECKING:
    from core.external_endpoints.adapters.base import BaseProtocolAdapter
    from core.external_endpoints.models import ExternalEndpoint


class ExternalIrisEngine(IrisEngineBase):
    """IrisEngineBase implementation backed by an external endpoint adapter."""

    def __init__(
        self,
        endpoint: "ExternalEndpoint",
        adapter: "BaseProtocolAdapter",
    ) -> None:
        self._endpoint = endpoint
        self._adapter = adapter
        self._adapter._engine_label = endpoint.name or "iris_bridge"
        self.display_name = f"{endpoint.display_label or endpoint.name} (Vision)"

    async def describe_image(
        self,
        file_path: str,
        mime_type: str | None = None,
        prompt: str | None = None,
        model: str | None = None,
    ) -> IrisResult | None:
        """Read *file_path* and forward image bytes to the external adapter."""
        if not os.path.exists(file_path):
            return None

        with open(file_path, "rb") as fh:
            image_bytes = fh.read()

        if not image_bytes:
            return None

        try:
            text = await self._adapter.describe_image(
                image_bytes,
                mime_type=mime_type,
                prompt=prompt,
                model=model or self._endpoint.default_model,
            )
        except Exception:
            return None

        if not text:
            return None

        return IrisResult(description=text, language=None)


# Required by IrisRegistry::load_engine() when loading via module path
ENGINE_CLASS = ExternalIrisEngine
