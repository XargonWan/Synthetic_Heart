"""An abandoned OpenAI-compatible generation must still close its record.

Live failure (2026-09-22 01:48:21 UTC, ``cortex_api:openai_compat:Venice2``):
a single generation request never returned. The chat turn sat on it for as long
as the process lived, and because the message queue has ONE consumer, every later
message sat unprocessed behind it — two of the user's messages were swallowed and
the only artefact left behind was a Langfuse trace whose ``output`` was NULL with
zero observations, which reads exactly like a request that was never made.

Cause: ``chat_completion`` recorded a response only on its success path and on
``except Exception``. A request abandoned by a per-message timeout, a superseded
background beat or a shutdown raises ``CancelledError``, which is a
``BaseException`` and therefore skipped straight past the logger — leaving the
cortex-API REQUEST line without its RESPONSE line (11 such orphans in the
deployment's log, this one user-facing). The Gemini adapter already handled this
with a status-499 branch; the OpenAI-compatible adapter did not.

These tests drive the real adapter entry point and assert the record is written,
for both the plain cancellation and the bridge's ``asyncio.wait_for`` timeout
shape, plus the negative case (a real error keeps its own reason).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import core.cortex_api_logger as cal
from core.external_endpoints.adapters.openai_compat import OpenAICompatAdapter


class _CapturingTrace:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def update(self, **kwargs: Any) -> None:
        self._captured.setdefault("trace_updates", []).append(kwargs)

    def generation(self, **kwargs: Any) -> None:
        self._captured.setdefault("generations", []).append(kwargs)


class _CapturingClient:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def trace(self, **kwargs: Any) -> _CapturingTrace:
        self._captured.setdefault("traces", []).append(kwargs)
        return _CapturingTrace(self._captured)

    def flush(self) -> None:
        return None


class _RaisingCompletions:
    def __init__(self, exc: BaseException | None) -> None:
        self._exc = exc

    async def create(self, **_kwargs: Any) -> Any:
        if self._exc is not None:
            raise self._exc
        # Never returns: the shape of a provider request that stalls.
        await asyncio.sleep(3600)


class _FakeClient:
    def __init__(self, exc: BaseException | None) -> None:
        self.chat = type("_Chat", (), {"completions": _RaisingCompletions(exc)})()


def _adapter(exc: BaseException | None) -> OpenAICompatAdapter:
    adapter = OpenAICompatAdapter("https://example.invalid/v1", "k", 5.0)
    adapter._engine_label = "TestEngine"
    adapter._get_client = lambda: _FakeClient(exc)  # type: ignore[method-assign]
    return adapter


def _capture(monkeypatch, captured: dict[str, Any], lines: list[str]) -> None:
    """Enable both recorders, capturing file lines and Langfuse payloads."""
    monkeypatch.setenv("CORTEX_API_LOG_ENABLED", "true")
    monkeypatch.setenv("CORTEX_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_FLUSH_EACH_CALL", "false")
    monkeypatch.setenv("CORTEX_LANGFUSE_CAPTURE_GENERATIONS", "true")

    class _DummyLogger:
        def debug(self, message: str) -> None:
            lines.append(message)

    monkeypatch.setattr(cal, "_get_logger", lambda: _DummyLogger())
    monkeypatch.setattr(cal, "_get_langfuse_client", lambda: _CapturingClient(captured))


def _trace_outputs(captured: dict[str, Any]) -> list[Any]:
    return [u["output"] for u in captured.get("trace_updates", []) if "output" in u]


def _response_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if "RESPONSE" in line]


def test_cancelled_generation_closes_the_langfuse_record(monkeypatch):
    captured: dict[str, Any] = {}
    lines: list[str] = []
    _capture(monkeypatch, captured, lines)
    adapter = _adapter(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            adapter.chat_completion([{"role": "user", "content": "hi"}], model="m")
        )

    outputs = _trace_outputs(captured)
    assert outputs, "an abandoned generation must still write a Langfuse output"
    assert outputs[-1], "the trace output must not be empty"
    assert "cancelled" in json.dumps(outputs[-1]).lower()

    responses = _response_lines(lines)
    assert responses, "the REQUEST line must be paired with a RESPONSE line"
    assert "status=499" in responses[-1]


def test_wait_for_timeout_closes_the_langfuse_record(monkeypatch):
    """The bridge wraps every generation in ``asyncio.wait_for``.

    A bridge-level timeout cancels the inner coroutine, so this is the shape a
    stalled provider request produces on the live path.
    """
    captured: dict[str, Any] = {}
    lines: list[str] = []
    _capture(monkeypatch, captured, lines)
    adapter = _adapter(None)  # create() never returns

    async def _run() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                adapter.chat_completion([{"role": "user", "content": "hi"}], model="m"),
                timeout=0.05,
            )

    asyncio.run(_run())

    outputs = _trace_outputs(captured)
    assert outputs and outputs[-1], "the timed-out request must still be recorded"
    assert "cancelled" in json.dumps(outputs[-1]).lower()
    assert "status=499" in _response_lines(lines)[-1]


def test_real_error_keeps_its_own_reason(monkeypatch):
    """Negative case: the new branches must not swallow a genuine failure."""
    captured: dict[str, Any] = {}
    lines: list[str] = []
    _capture(monkeypatch, captured, lines)
    adapter = _adapter(ValueError("provider said no"))

    with pytest.raises(ValueError):
        asyncio.run(
            adapter.chat_completion([{"role": "user", "content": "hi"}], model="m")
        )

    outputs = _trace_outputs(captured)
    assert outputs and "provider said no" in json.dumps(outputs[-1])
    response = _response_lines(lines)[-1]
    assert "status=499" not in response
    assert "provider said no" in response
