"""
tests/test_grillo_llm_failure_recovery.py

Unit tests for the G.R.I.L.L.O. LLM-Failure Recovery plugin.

These tests verify:
- failures are detected and recovered (re-injection into message_chain)
- the anti-loop guarantee: every processed failure is marked processed even
  when regeneration fails
- rate limiting per interface_path
- graceful handling when original text is unavailable
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.grillo.grillo_llm_failure_recovery import (
    GrilloLLMFailureRecoveryPlugin,
    _coerce_datetime,
)


def _make_entry(
    *,
    id: int = 1,
    interface_path: str = "telegram_bot/123",
    content_preview: str = "hello there",
    created_at=None,
    metadata: dict | None = None,
) -> dict:
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    return {
        "id": id,
        "failure_code": "timeout",
        "stage": "llm_fallback",
        "reason": "timeout after 240s",
        "interface_path": interface_path,
        "chat_id": "123",
        "thread_id": None,
        "engine": "selenium-llm-engine",
        "model": "gemini",
        "message_id": None,
        "content_preview": content_preview,
        "metadata": metadata or {},
        "created_at": created_at,
    }


@pytest.fixture
def plugin():
    p = GrilloLLMFailureRecoveryPlugin()
    p._running = True
    yield p
    p._running = False


@pytest.mark.asyncio
async def test_recovers_failure_by_reinjecting_original_text(plugin):
    """A failure with content_preview should re-inject that text."""
    entry = _make_entry(id=10, content_preview="what is 2+2?")

    sent = {}

    async def fake_handle(bot, message, text, *, source="user", context=None):
        sent["text"] = text
        sent["context"] = context
        return "ACTIONS_EXECUTED"

    fake_bot = MagicMock()
    with (
        patch(
            "core.llm_failure_log.list_failure_entries",
            AsyncMock(return_value={"entries": [entry]}),
        ),
        patch(
            "core.llm_failure_log.mark_failure_processed", AsyncMock(return_value=True)
        ),
        patch(
            "core.message_chain.handle_incoming_message",
            fake_handle,
        ),
        patch.object(plugin, "_get_bot", AsyncMock(return_value=fake_bot)),
    ):
        await plugin._recover_one(
            entry,
            datetime.now(timezone.utc),
            __import__("datetime").timedelta(minutes=30),
        )

    assert sent["text"] == "what is 2+2?"
    assert sent["context"]["grillo_recovery"] is True
    assert sent["context"]["skip_history"] is True


@pytest.mark.asyncio
async def test_failure_marked_processed_even_if_recovery_raises(plugin):
    """Anti-loop guarantee: failure is marked processed even when recovery fails."""
    entry = _make_entry(id=20, content_preview="retry me")

    mark_mock = AsyncMock(return_value=True)
    with (
        patch(
            "core.llm_failure_log.list_failure_entries",
            AsyncMock(return_value={"entries": [entry]}),
        ),
        patch("core.llm_failure_log.mark_failure_processed", mark_mock),
        patch.object(
            plugin, "_do_recover", AsyncMock(side_effect=RuntimeError("boom"))
        ),
    ):
        await plugin._recover_one(
            entry,
            datetime.now(timezone.utc),
            __import__("datetime").timedelta(minutes=30),
        )

    mark_mock.assert_awaited_once_with(20)
    assert 20 in plugin._processed_ids


@pytest.mark.asyncio
async def test_old_failure_is_marked_processed_and_skipped(plugin):
    """Failures older than the window are marked processed without recovery."""
    old = datetime.now(timezone.utc) - __import__("datetime").timedelta(minutes=90)
    entry = _make_entry(id=30, content_preview="ancient", created_at=old)

    mark_mock = AsyncMock(return_value=True)
    recover_mock = AsyncMock()
    with (
        patch(
            "core.llm_failure_log.list_failure_entries",
            AsyncMock(return_value={"entries": [entry]}),
        ),
        patch("core.llm_failure_log.mark_failure_processed", mark_mock),
        patch.object(plugin, "_do_recover", recover_mock),
    ):
        await plugin._recover_one(
            entry,
            datetime.now(timezone.utc),
            __import__("datetime").timedelta(minutes=30),
        )

    mark_mock.assert_awaited_once_with(30)
    recover_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limit_blocks_second_recovery_same_interface(plugin):
    """Only one recovery per interface_path per window."""
    e1 = _make_entry(id=40, interface_path="telegram_bot/999", content_preview="a")
    e2 = _make_entry(id=41, interface_path="telegram_bot/999", content_preview="b")

    recover_mock = AsyncMock()
    with (
        patch(
            "core.llm_failure_log.list_failure_entries",
            AsyncMock(return_value={"entries": [e1, e2]}),
        ),
        patch(
            "core.llm_failure_log.mark_failure_processed", AsyncMock(return_value=True)
        ),
        patch.object(plugin, "_do_recover", recover_mock),
    ):
        now = datetime.now(timezone.utc)
        window = __import__("datetime").timedelta(minutes=30)
        await plugin._recover_one(e1, now, window)
        await plugin._recover_one(e2, now, window)

    # Second one is rate-limited (last_recovery was just set), so only 1 recover.
    assert recover_mock.await_count == 1


@pytest.mark.asyncio
async def test_recovers_without_original_text_uses_history(plugin):
    """When content_preview is empty, fall back to last user message in history."""
    entry = _make_entry(id=50, interface_path="telegram_bot/555", content_preview="")

    sent = {}

    async def fake_handle(bot, message, text, *, source="user", context=None):
        sent["text"] = text
        return "ACTIONS_EXECUTED"

    fake_bot = MagicMock()
    with (
        patch(
            "core.llm_failure_log.list_failure_entries",
            AsyncMock(return_value={"entries": [entry]}),
        ),
        patch(
            "core.llm_failure_log.mark_failure_processed", AsyncMock(return_value=True)
        ),
        patch(
            "core.message_chain.handle_incoming_message",
            fake_handle,
        ),
        patch.object(plugin, "_get_bot", AsyncMock(return_value=fake_bot)),
        patch(
            "core.chat_history_cache.get_last_message",
            AsyncMock(
                return_value={
                    "sender_id": "123",
                    "text": "tell me a joke",
                }
            ),
        ),
    ):
        await plugin._recover_one(
            entry,
            datetime.now(timezone.utc),
            __import__("datetime").timedelta(minutes=30),
        )

    # Original text recovered from history -> re-injected verbatim.
    assert sent["text"] == "tell me a joke"


def test_coerce_datetime_variants():
    assert _coerce_datetime(None) is None
    assert isinstance(_coerce_datetime(datetime.now()), datetime)
    assert isinstance(_coerce_datetime("2026-07-18T10:00:00+00:00"), datetime)
    assert _coerce_datetime("not-a-date") is None
