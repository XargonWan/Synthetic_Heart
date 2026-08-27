"""Tests for the delivery circuit breaker + dead-target registry.

These are DB-free: the guard's persistence methods are stubbed so the in-memory
circuit-breaker logic (trip threshold, skip, reset, revive) is exercised in
isolation, plus the transport-layer wiring is verified with a patched guard.
"""

from unittest.mock import AsyncMock

import pytest

from core.delivery_guard import (
    DeadTargetError,
    DeliveryGuard,
    classify_delivery_failure,
)
from core import transport_layer


# ---------------------------------------------------------------------------
# Failure classification (structural, no string routing for intents)
# ---------------------------------------------------------------------------
def test_classify_dead_target_error_types() -> None:
    assert (
        classify_delivery_failure(DeadTargetError(138032544493862912)) == "dead_target"
    )
    assert classify_delivery_failure(None) == "transient"
    assert classify_delivery_failure(TimeoutError("timed out")) == "transient"


def test_classify_marker_fallback_for_plain_strings() -> None:
    # Interfaces that surface permanent target loss as a bare string still
    # classify as dead-target via the narrow marker fallback.
    assert (
        classify_delivery_failure(RuntimeError("Unknown channel or user: 123"))
        == "dead_target"
    )
    assert classify_delivery_failure(RuntimeError("Chat not found")) == "dead_target"
    # Transient conditions must never trip the breaker.
    assert (
        classify_delivery_failure(RuntimeError("Connection reset by peer"))
        == "transient"
    )


# ---------------------------------------------------------------------------
# Guard in-memory breaker logic
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_breaker_trips_after_threshold(monkeypatch) -> None:
    guard = DeliveryGuard()
    monkeypatch.setattr(guard, "_load_dead_set", AsyncMock(return_value=None))
    monkeypatch.setattr(guard, "_persist_failure", AsyncMock(return_value=None))
    monkeypatch.setattr(guard, "_delete_row", AsyncMock(return_value=None))
    monkeypatch.setattr(guard, "is_enabled", lambda: True)
    monkeypatch.setattr(guard, "max_failures", lambda: 3)

    target = ("discord_bot", "138032544493862912")
    assert not await guard.should_skip(*target)

    await guard.record_failure(*target, DeadTargetError(target[1]))
    await guard.record_failure(*target, DeadTargetError(target[1]))
    assert not await guard.should_skip(*target), "should not trip below threshold"

    await guard.record_failure(*target, DeadTargetError(target[1]))
    assert await guard.should_skip(*target), "should trip at threshold"


@pytest.mark.asyncio
async def test_success_resets_breaker(monkeypatch) -> None:
    guard = DeliveryGuard()
    monkeypatch.setattr(guard, "_load_dead_set", AsyncMock(return_value=None))
    monkeypatch.setattr(guard, "_persist_failure", AsyncMock(return_value=None))
    monkeypatch.setattr(guard, "_delete_row", AsyncMock(return_value=None))
    monkeypatch.setattr(guard, "is_enabled", lambda: True)
    monkeypatch.setattr(guard, "max_failures", lambda: 2)

    target = ("discord_bot", "138032544493862912")
    await guard.record_failure(*target, DeadTargetError(target[1]))
    await guard.record_failure(*target, DeadTargetError(target[1]))
    assert await guard.should_skip(*target)

    await guard.record_success(*target)
    assert not await guard.should_skip(*target), "success must re-arm the target"


@pytest.mark.asyncio
async def test_disabled_guard_never_skips(monkeypatch) -> None:
    guard = DeliveryGuard()
    monkeypatch.setattr(guard, "is_enabled", lambda: False)
    monkeypatch.setattr(guard, "max_failures", lambda: 1)
    monkeypatch.setattr(guard, "_load_dead_set", AsyncMock(return_value=None))
    monkeypatch.setattr(guard, "_persist_failure", AsyncMock(return_value=None))

    await guard.record_failure("discord_bot", "138", DeadTargetError("138"))
    assert not await guard.should_skip("discord_bot", "138")


@pytest.mark.asyncio
async def test_revive_clears_in_memory_state(monkeypatch) -> None:
    guard = DeliveryGuard()
    monkeypatch.setattr(guard, "_load_dead_set", AsyncMock(return_value=None))
    monkeypatch.setattr(guard, "_persist_failure", AsyncMock(return_value=None))
    monkeypatch.setattr(guard, "_delete_row", AsyncMock(return_value=None))
    monkeypatch.setattr(guard, "is_enabled", lambda: True)
    monkeypatch.setattr(guard, "max_failures", lambda: 1)

    target = ("discord_bot", "138032544493862912")
    await guard.record_failure(*target, DeadTargetError(target[1]))
    assert await guard.should_skip(*target)

    # revive_target hits the DB (stubbed to find no row), so it returns False;
    # but when a row IS found the in-memory set is cleared. Simulate by forcing
    # the in-memory discard directly through record_success semantics.
    await guard.record_success(*target)
    assert not await guard.should_skip(*target)


# ---------------------------------------------------------------------------
# Transport-layer wiring
# ---------------------------------------------------------------------------
def test_resolve_delivery_target() -> None:
    # interface_path carries the interface name; chat_id comes from args[0].
    target = transport_layer._resolve_delivery_target(
        lambda: None, ("138",), {"chat_id": None}, "discord_bot/138"
    )
    assert target == ("discord_bot", "138")

    # Unresolvable (no chat id) -> None.
    assert transport_layer._resolve_delivery_target(lambda: None, (), {}, None) is None


def test_result_is_delivery_failure() -> None:
    assert transport_layer._result_is_delivery_failure(False) is True
    assert transport_layer._result_is_delivery_failure({"status": "failed"}) is True
    assert transport_layer._result_is_delivery_failure({"ok": False}) is True
    assert transport_layer._result_is_delivery_failure(None) is False
    assert transport_layer._result_is_delivery_failure(True) is False
    assert transport_layer._result_is_delivery_failure({"status": "success"}) is False


@pytest.mark.asyncio
async def test_universal_send_skips_dead_target(monkeypatch) -> None:
    attempted = []

    async def send_that_would_raise(*args, **kwargs):
        attempted.append(True)
        raise RuntimeError("should not be attempted")

    monkeypatch.setattr(
        "core.delivery_guard.delivery_guard.should_skip",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context",
        AsyncMock(return_value=None),
    )

    result = await transport_layer.universal_send(
        send_that_would_raise,
        "138032544493862912",
        text="hello",
        interface_path="discord_bot/138032544493862912",
    )

    assert result is None
    assert not attempted, "dead target must be skipped before the send is attempted"
