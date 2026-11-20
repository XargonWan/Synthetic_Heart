"""Send one photo-sized image through the Iris path and report what came back.

Used by the WebUI's "Test Vision" button (one model) and by the bulk probe that
decides which models the Iris selector offers. It goes through
``ExternalIrisEngine`` exactly as a chat photo does: the same adapter code, the
Iris default prompt and the engine's own deadline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from core.vision_probe_image import build_probe_photo

# A probe slower than this still passes but is flagged "slow": a chat photo is cut
# at 120 s, and an answer near that is a stall waiting to happen.
SLOW_MS = 30_000
_HANG_MARGIN_SECONDS = 10.0


@dataclass
class VisionProbeResult:
    """Outcome of one image probe of one model."""

    model: str
    ok: bool
    elapsed_ms: int
    image_bytes: int
    description: str = ""
    error: str = ""

    @property
    def slow(self) -> bool:
        return self.ok and self.elapsed_ms > SLOW_MS


def _iris_prompt() -> str:
    try:
        from plugins.iris_plugin.iris_plugin import _IRIS_DEFAULT_PROMPT_TEXT

        return str(_IRIS_DEFAULT_PROMPT_TEXT)
    except Exception:
        return "Describe this image."


def effective_model(endpoint: Any, model: str | None) -> str:
    """The model a probe will test: the one asked for, else what a chat photo uses."""
    return (
        model
        or (endpoint.extra_config or {}).get("iris_model")
        or endpoint.default_model
        or ""
    )


async def run_vision_probe(
    endpoint: Any,
    adapter: Any,
    model: str | None,
    *,
    timeout_seconds: float | None = None,
) -> VisionProbeResult:
    """Probe ``model`` of ``endpoint`` with a photo; never raises.

    ``timeout_seconds`` replaces the engine's own deadline (the bulk probe uses a
    short one so a model that cannot answer images does not hold the run for two
    minutes). A model that was asked for explicitly is tested as such: the
    endpoint's ``iris_model`` would otherwise take precedence inside the engine.
    """
    from core.external_endpoints.bridges.iris_bridge import ExternalIrisEngine

    extra = dict(endpoint.extra_config or {})
    if model:
        extra.pop("iris_model", None)
    if timeout_seconds is not None:
        extra["timeout"] = timeout_seconds
    engine = ExternalIrisEngine(
        dataclasses.replace(endpoint, extra_config=extra), adapter
    )
    tested = effective_model(endpoint, model)
    image = build_probe_photo()
    deadline = engine._get_request_timeout() + _HANG_MARGIN_SECONDS
    started = time.monotonic()
    path = ""
    result = None
    error = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(image)
            path = tmp.name
        result = await asyncio.wait_for(
            engine.describe_image(
                path, "image/jpeg", _iris_prompt(), model=tested or None
            ),
            timeout=deadline,
        )
    except asyncio.TimeoutError:
        result = None
    except Exception as exc:
        error = str(exc)
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    elapsed_ms = int((time.monotonic() - started) * 1000)
    description = ((result.description if result else "") or "").strip()
    if description:
        return VisionProbeResult(tested, True, elapsed_ms, len(image), description)
    return VisionProbeResult(
        tested,
        False,
        elapsed_ms,
        len(image),
        error=error
        or (
            f"No description after {elapsed_ms / 1000:.0f}s for a "
            f"{len(image) // 1024} KB photo — timed out or the model/endpoint does "
            "not accept images (see the log)"
        ),
    )
