# core/external_endpoints/bridges/iris_bridge.py
"""Iris (vision) bridge for external endpoints."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from core.logging_utils import log_warning
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

    def _get_request_timeout(self) -> float:
        """Get the request timeout from endpoint extra_config or use a safe default."""
        extra = self._endpoint.extra_config or {}
        timeout = extra.get("timeout")
        if timeout is not None:
            try:
                return float(timeout)
            except (ValueError, TypeError):
                pass
        return 120.0

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

        # Resolve the vision model for this endpoint.  Multi-modal endpoints
        # (e.g. Harmony) use a dedicated ``iris_model`` in ``extra_config``
        # because the endpoint's ``default_model`` is reserved for the
        # cortex/text engine and the caller-supplied ``model`` may be a global
        # ``IRIS_DEFAULT_MODEL`` belonging to a different engine (and thus not a
        # valid image-description model, e.g. an audio-conversion model).
        extra = self._endpoint.extra_config or {}
        vision_model = extra.get("iris_model") or model or self._endpoint.default_model

        request_timeout = self._get_request_timeout()
        try:
            text = await asyncio.wait_for(
                self._adapter.describe_image(
                    image_bytes,
                    mime_type=mime_type,
                    prompt=prompt,
                    model=vision_model,
                ),
                timeout=request_timeout,
            )
        except asyncio.TimeoutError:
            log_warning(
                f"[iris_bridge:{self._endpoint.name}] describe_image timed out "
                f"after {request_timeout}s"
            )
            return None
        except Exception as exc:
            log_warning(
                f"[iris_bridge:{self._endpoint.name}] describe_image failed "
                f"(model={vision_model}, mime={mime_type}): {exc!r}"
            )
            return None

        if not text:
            return None

        return IrisResult(description=text, language=None)


# Required by IrisRegistry::load_engine() when loading via module path
ENGINE_CLASS = ExternalIrisEngine
