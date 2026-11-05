"""Regression tests for the malformed-completion guard in OpenAICompatAdapter.

Live failure (agent cortex 'harmonyai', 2026-08-26): an OpenAI-compatible
server answered HTTP 200 with a body that is not a well-formed completion
(missing ``choices``), and the unguarded ``response.choices[0]`` crashed the
whole engine call with the opaque ``TypeError: 'NoneType' object is not
subscriptable``. The adapter must surface a descriptive error instead, the
bridge must record it in its diagnostics, and the agent-loop classifier must
recognise such internal crashes as ``internal`` rather than the catch-all
``unknown``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.external_endpoints.adapters.openai_compat import OpenAICompatAdapter
from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
from core.external_endpoints.models import EndpointProtocol


class _FakeCompletionsApi:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def create(self, **kwargs: Any) -> Any:
        return self._response


class _FakeChatApi:
    def __init__(self, response: Any) -> None:
        self.completions = _FakeCompletionsApi(response)


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.chat = _FakeChatApi(response)


def _make_adapter(response: Any) -> OpenAICompatAdapter:
    adapter = OpenAICompatAdapter(base_url="http://127.0.0.1:9/v1")
    adapter._client = _FakeClient(response)
    return adapter


def _make_endpoint() -> Any:
    return SimpleNamespace(
        name="test-endpoint",
        display_label="Test Endpoint",
        default_model="test-model",
        available_models=[],
        extra_config={"retry_attempts": 1},
        protocol=EndpointProtocol.OPENAI,
    )


@pytest.mark.asyncio
async def test_missing_choices_raises_descriptive_error_not_typeerror() -> None:
    """``choices=None`` must yield a clear RuntimeError naming the real cause."""
    malformed = SimpleNamespace(choices=None, model="harmony-model", usage=None)
    adapter = _make_adapter(malformed)

    with pytest.raises(RuntimeError) as excinfo:
        await adapter.chat_completion(
            [{"role": "user", "content": "hi"}], model="harmony-model"
        )

    message = str(excinfo.value)
    assert "no completion choices" in message
    assert "malformed or non-standard response" in message
    # The raw payload preview aids diagnosis (what did the server send?).
    assert "harmony-model" in message


@pytest.mark.asyncio
async def test_empty_choices_list_also_guarded() -> None:
    malformed = SimpleNamespace(choices=[], model="m", usage=None)
    adapter = _make_adapter(malformed)

    with pytest.raises(RuntimeError) as excinfo:
        await adapter.chat_completion([{"role": "user", "content": "hi"}], model="m")

    assert "no completion choices" in str(excinfo.value)


@pytest.mark.asyncio
async def test_bridge_records_descriptive_error_for_malformed_body() -> None:
    """End to end through the bridge: diagnostics carry the real cause.

    The malformed-body error is deliberately worded so it is NOT retryable —
    a non-standard reply shape will not heal on an identical re-request.
    """
    malformed = SimpleNamespace(choices=None, model="test-model", usage=None)
    bridge = ExternalCortexEngine(
        endpoint=_make_endpoint(), adapter=_make_adapter(malformed)
    )

    with pytest.raises(RuntimeError) as excinfo:
        await bridge.generate_response([{"role": "user", "content": "hi"}])

    assert "no completion choices" in str(excinfo.value)
    recorded = bridge._last_attempt_error
    assert recorded is not None
    # Recorded verbatim as "TypeName: message" for classify_engine_failure.
    assert recorded.startswith("RuntimeError:")
    assert "no completion choices" in recorded
