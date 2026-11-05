"""The Langfuse flush must never run on the caller's thread.

Live symptom (2026-09-18): ``flush after cortex API request took 40609ms`` while
the engine itself answered in 3.7 s — every reply was ~4x slower than usual
because the turn waited for the Langfuse ingestion pipeline to acknowledge the
events. Nothing in the turn needs that acknowledgement.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import core.cortex_api_logger as logger_mod


class _SlowClient:
    """Minimal Langfuse-client stand-in with a slow, observable flush."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0
        self.started = threading.Event()
        self.finished = threading.Event()

    def flush(self) -> None:
        self.calls += 1
        self.started.set()
        time.sleep(self.delay)
        self.finished.set()


def _reset_flush_state() -> None:
    with logger_mod._FLUSH_LOCK:
        logger_mod._FLUSH_IN_FLIGHT = False


def test_flush_returns_immediately_while_the_worker_runs() -> None:
    _reset_flush_state()
    client = _SlowClient(0.6)

    started = time.monotonic()
    logger_mod._flush_langfuse_client(client, context="test")
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, f"flush blocked the caller for {elapsed:.2f}s"
    assert client.finished.wait(5.0), "the background flush never ran"


def test_second_flush_does_not_stack_a_second_worker() -> None:
    _reset_flush_state()
    client = _SlowClient(0.4)

    logger_mod._flush_langfuse_client(client, context="test")
    assert client.started.wait(2.0), "the first flush never started"
    logger_mod._flush_langfuse_client(client, context="test")
    assert client.finished.wait(5.0)
    time.sleep(0.05)

    assert client.calls == 1


def test_slow_flush_is_still_reported_from_the_worker(monkeypatch: Any) -> None:
    _reset_flush_state()
    warnings: list[tuple[Any, ...]] = []

    class _Recorder:
        def warning(self, *args: Any) -> None:
            warnings.append(args)

    monkeypatch.setattr(logger_mod, "_LANGFUSE_SLOW_FLUSH_MS", 10)
    monkeypatch.setattr(logger_mod, "_get_runtime_logger", lambda: _Recorder())
    monkeypatch.setattr(
        logger_mod,
        "_langfuse_client_health",
        lambda client: "queue=0 consumers=1 dead=0",
    )

    client = _SlowClient(0.15)
    logger_mod._flush_langfuse_client(client, context="cortex API request")
    assert client.finished.wait(5.0)

    for _ in range(100):
        if warnings:
            break
        time.sleep(0.02)

    assert warnings, "a slow flush must stay visible"
    assert "flush after %s took" in warnings[0][0]
    assert warnings[0][1] == "cortex API request"


def test_missing_client_flush_is_harmless(monkeypatch: Any) -> None:
    _reset_flush_state()
    monkeypatch.setattr(
        logger_mod,
        "_langfuse_client_health",
        lambda client: "queue=0 consumers=1 dead=0",
    )

    logger_mod._flush_langfuse_client(object(), context="test")
    time.sleep(0.2)

    with logger_mod._FLUSH_LOCK:
        assert logger_mod._FLUSH_IN_FLIGHT is False
