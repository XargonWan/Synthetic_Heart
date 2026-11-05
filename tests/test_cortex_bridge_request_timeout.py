"""The per-request timeout resolution in the external-endpoint bridge.

A caller that brings its own budget (the debrief asks for 120 s, recon passes
RECON_TIMEOUT, the agent loop passes its per-call budget) must get that budget,
clamped to ``LLM_MAX_REQUEST_TIMEOUT_SEC``; a caller that brings none must keep
the endpoint's default. The adapter kwarg AND the ``asyncio.wait_for`` guard
have to use the same resolved value: until this was fixed the guard always used
the endpoint's default, so a caller asking for more than the endpoint allowed
was silently cut at the endpoint's value.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from core.external_endpoints.bridges import cortex_bridge
from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
from core.external_endpoints.models import EndpointProtocol

CEILING = 120.0
ENDPOINT_TIMEOUT = 30


def _make_endpoint(*, extra_config: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name="test-endpoint",
        display_label="Test Endpoint",
        default_model="test-model",
        available_models=[],
        extra_config=extra_config
        if extra_config is not None
        else {"timeout": ENDPOINT_TIMEOUT},
        protocol=EndpointProtocol.OPENAI,
    )


def _make_chat_response(*, content: str = '{"actions": []}') -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        model="test-model",
        finish_reason="stop",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


class _RecordingAdapter:
    """Adapter that records the kwargs of every completion request."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self._delay = delay
        self._last_completion_metadata: dict[str, Any] = {}
        self.sent_timeouts: list[float] = []
        self.chat_completion = AsyncMock(side_effect=self._complete)

    async def _complete(
        self, msg_list: list[dict[str, Any]], **kwargs: Any
    ) -> SimpleNamespace:
        self.sent_timeouts.append(float(kwargs["timeout"]))
        if self._delay:
            await asyncio.sleep(self._delay)
        return _make_chat_response()

    @property
    def sent_timeout(self) -> float:
        return self.sent_timeouts[-1]


def _make_bridge(
    *, extra_config: dict[str, Any] | None = None, delay: float = 0.0
) -> tuple[ExternalCortexEngine, _RecordingAdapter]:
    adapter = _RecordingAdapter(delay=delay)
    bridge = ExternalCortexEngine(
        endpoint=cast(Any, _make_endpoint(extra_config=extra_config)),
        adapter=cast(Any, adapter),
    )
    # The ceiling is a live config value; pin it so the test reads no live state.
    bridge._get_max_request_timeout = lambda: CEILING  # type: ignore[method-assign]
    return bridge, adapter


@pytest.fixture
def guard_timeouts(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the timeout every ``asyncio.wait_for`` guard is called with."""

    seen: list[float] = []

    async def _fake_wait_for(
        awaitable: Any, timeout: float | None = None, **_: Any
    ) -> Any:
        seen.append(float(timeout))  # type: ignore[arg-type]
        return await awaitable

    monkeypatch.setattr(cortex_bridge.asyncio, "wait_for", _fake_wait_for)
    return seen


@pytest.mark.asyncio
async def test_caller_budget_is_honoured_over_the_endpoint_default(
    guard_timeouts: list[float],
) -> None:
    """The debrief asks for 120 s against a 30 s endpoint and must get 120 s."""
    bridge, adapter = _make_bridge()

    await bridge.generate_response([{"role": "user", "content": "hi"}], timeout=120)

    assert adapter.sent_timeout == 120.0
    assert guard_timeouts == [120.0]


@pytest.mark.asyncio
async def test_caller_budget_above_the_ceiling_is_clamped(
    guard_timeouts: list[float],
) -> None:
    """An agent-loop budget of 1800 s cannot re-introduce a multi-minute wedge."""
    bridge, adapter = _make_bridge()

    await bridge.generate_response([{"role": "user", "content": "hi"}], timeout=1800)

    assert adapter.sent_timeout == CEILING
    assert guard_timeouts == [CEILING]


@pytest.mark.asyncio
async def test_endpoint_default_applies_without_a_caller_budget(
    guard_timeouts: list[float],
) -> None:
    """Ordinary callers (chat turns, Grillo beats) keep the endpoint's cap."""
    bridge, adapter = _make_bridge()

    await bridge.generate_response([{"role": "user", "content": "hi"}])

    assert adapter.sent_timeout == float(ENDPOINT_TIMEOUT)
    assert guard_timeouts == [float(ENDPOINT_TIMEOUT)]


@pytest.mark.asyncio
@pytest.mark.parametrize("unusable", [0, -5, "not-a-number", None])
async def test_unusable_caller_budget_falls_back_to_the_endpoint_default(
    unusable: Any, guard_timeouts: list[float]
) -> None:
    bridge, adapter = _make_bridge()

    await bridge.generate_response(
        [{"role": "user", "content": "hi"}], timeout=unusable
    )

    assert adapter.sent_timeout == float(ENDPOINT_TIMEOUT)
    assert guard_timeouts == [float(ENDPOINT_TIMEOUT)]


@pytest.mark.asyncio
async def test_the_guard_itself_honours_a_short_caller_budget() -> None:
    """Behavioural proof, with the REAL guard: a 1 s budget cuts a 5 s call.

    The floor of the resolver is 1 s, so this is the shortest budget a caller can
    ask for; the point is that the guard - not just the adapter kwarg - uses it.
    """
    bridge, _ = _make_bridge(delay=5.0)

    with pytest.raises(TimeoutError) as excinfo:
        await bridge.generate_response([{"role": "user", "content": "hi"}], timeout=1)

    assert "1.0s" in str(excinfo.value)


@pytest.mark.asyncio
async def test_endpoint_without_a_timeout_uses_the_global_default(
    guard_timeouts: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No per-endpoint timeout: the global generation budget still applies."""
    bridge, adapter = _make_bridge(extra_config={})
    monkeypatch.setattr(bridge, "_get_request_timeout", lambda: 900.0)

    await bridge.generate_response([{"role": "user", "content": "hi"}])

    assert adapter.sent_timeout == 900.0
    assert guard_timeouts == [900.0]
