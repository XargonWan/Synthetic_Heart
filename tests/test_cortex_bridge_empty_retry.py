from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
from core.external_endpoints.models import EndpointProtocol


def _make_endpoint(*, extra_config: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name="test-endpoint",
        display_label="Test Endpoint",
        default_model="test-model",
        available_models=[],
        extra_config=extra_config or {},
        protocol=EndpointProtocol.OPENAI,
    )


def _make_chat_response(*, content: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        model="test-model",
        finish_reason="stop",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


class _RecordingAdapter:
    """Adapter that returns a scripted sequence of chat responses."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = responses
        self._last_completion_metadata: dict[str, Any] = {}
        self.calls: list[list[dict[str, Any]]] = []
        self.chat_completion = AsyncMock(side_effect=self._scripted, wraps=lambda: None)

    async def _scripted(
        self, msg_list: list[dict[str, Any]], **kwargs: Any
    ) -> SimpleNamespace:
        self.calls.append(list(msg_list))
        if not self._responses:
            raise AssertionError("no more scripted responses")
        return self._responses.pop(0)


def _make_bridge(adapter: Any) -> ExternalCortexEngine:
    endpoint = _make_endpoint()
    return ExternalCortexEngine(endpoint=endpoint, adapter=adapter)


@pytest.mark.asyncio
async def test_empty_response_triggers_retry_and_returns_second_content() -> None:
    adapter = _RecordingAdapter(
        [
            _make_chat_response(content=""),
            _make_chat_response(content='{"actions": []}'),
        ]
    )
    bridge = _make_bridge(adapter)

    result = await bridge.generate_response([{"role": "user", "content": "hi"}])

    assert result == '{"actions": []}'
    # The request was re-sent (2 chat_completion calls), both with the same
    # message list — no mutation/duplication of the input on the wire.
    assert adapter.chat_completion.await_count == 2
    assert adapter.calls[0] == adapter.calls[1]


@pytest.mark.asyncio
async def test_empty_response_returns_empty_when_retries_exhausted() -> None:
    adapter = _RecordingAdapter(
        [
            _make_chat_response(content=""),
            _make_chat_response(content=""),
            _make_chat_response(content=""),
            _make_chat_response(content=""),
        ]
    )
    bridge = _make_bridge(adapter)

    # retry_attempts defaults to 3, giving 3 total attempts (1 initial + 2
    # retries, matching the timeout-retry convention), then the empty result is
    # returned unchanged (no crash, no infinite loop).
    result = await bridge.generate_response([{"role": "user", "content": "hi"}])

    assert result == ""
    assert adapter.chat_completion.await_count == 3


@pytest.mark.asyncio
async def test_empty_retry_respects_disabled_flag() -> None:
    adapter = _RecordingAdapter([_make_chat_response(content="")])
    endpoint = _make_endpoint(extra_config={"retry_on_empty": False})
    bridge = ExternalCortexEngine(endpoint=endpoint, adapter=adapter)

    result = await bridge.generate_response([{"role": "user", "content": "hi"}])

    assert result == ""
    assert adapter.chat_completion.await_count == 1


@pytest.mark.asyncio
async def test_non_empty_response_no_retry() -> None:
    adapter = _RecordingAdapter([_make_chat_response(content="fine")])
    bridge = _make_bridge(adapter)

    result = await bridge.generate_response([{"role": "user", "content": "hi"}])

    assert result == "fine"
    assert adapter.chat_completion.await_count == 1
