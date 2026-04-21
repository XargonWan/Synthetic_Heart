"""
Test Grillo beat system functionality.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from typing import Tuple


def _create_mock_db_context() -> Tuple[MagicMock, AsyncMock]:
    """Create a mock database context manager."""
    mock_cursor = AsyncMock()
    mock_cursor.fetchall = AsyncMock(return_value=[])
    mock_cursor.fetchone = AsyncMock(return_value=None)
    mock_cursor.execute = AsyncMock()
    mock_cursor.lastrowid = 999

    mock_conn = AsyncMock()
    mock_conn.cursor = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_cursor),
            __aexit__=AsyncMock(),
        )
    )
    mock_conn.commit = AsyncMock()

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    return mock_ctx, mock_cursor


@pytest.mark.asyncio
async def test_grillo_beat_types_exist() -> None:
    """Test that Grillo has all expected beat types defined."""
    from plugins.grillo.grillo_impl import GrilloPlugin

    plugin = GrilloPlugin()

    # Verify beat types are defined
    expected_types = [
        "tag_elaboration",
        "memory_consolidation",
        "diary_consolidation",
        "self_reflection",
        "curiosity",
        "relationship",
    ]

    # Check _select_beat_type returns one of the expected types
    beat_type = plugin._select_beat_type()
    assert beat_type in expected_types


@pytest.mark.asyncio
async def test_grillo_set_activity_response_text_with_valid_id() -> None:
    """Test that set_activity_response_text handles valid inputs correctly."""
    mock_ctx, _ = _create_mock_db_context()

    def mock_get_conn_ctx() -> MagicMock:
        return mock_ctx

    import core.db

    original = core.db.get_conn_ctx
    core.db.get_conn_ctx = mock_get_conn_ctx  # type: ignore[assignment]

    try:
        from plugins.grillo.grillo_impl import GrilloPlugin

        # Should not raise
        await GrilloPlugin.set_activity_response_text(
            activity_log_id=123, response_text="Test response"
        )
    finally:
        core.db.get_conn_ctx = original


@pytest.mark.asyncio
async def test_grillo_set_activity_response_text_with_none_id() -> None:
    """Test that set_activity_response_text handles None activity_log_id."""
    from plugins.grillo.grillo_impl import GrilloPlugin

    # Should return early without error
    await GrilloPlugin.set_activity_response_text(
        activity_log_id=0,
        response_text="Test response",
    )


@pytest.mark.asyncio
async def test_grillo_outreach_prompt_generation() -> None:
    """Test that Grillo outreach generates proper prompts."""
    from plugins.grillo.grillo_outreach import GrilloOutreachPlugin

    plugin = GrilloOutreachPlugin()

    prompt = plugin._build_outreach_prompt(
        interface="telegram_bot",
        chat_id="123456",
        context=["Test context 1", "Test context 2"],
    )

    assert "G.R.I.L.L.O. OUTREACH" in prompt
    assert "message_telegram_bot" in prompt
    assert "Test context 1" in prompt


@pytest.mark.asyncio
async def test_grillo_response_extraction() -> None:
    """Test response text extraction from various formats."""
    from plugins.grillo.grillo_response_recorder import (
        extract_response_text_from_cortex_response,
    )

    result = await extract_response_text_from_cortex_response("Simple string response")
    assert result == "Simple string response"

    result = await extract_response_text_from_cortex_response(
        {"message": "Dict message"}
    )
    assert result == "Dict message"

    result = await extract_response_text_from_cortex_response(
        {"content": "Dict content"}
    )
    assert result == "Dict content"

    result = await extract_response_text_from_cortex_response(
        {"actions": [{"type": "message", "payload": {"text": "Action text"}}]}
    )
    assert "Action text" in result

    result = await extract_response_text_from_cortex_response(None)
    assert result == ""


@pytest.mark.asyncio
async def test_grillo_activity_log_creation() -> None:
    """Test activity log creation."""
    mock_ctx, _ = _create_mock_db_context()

    def mock_get_conn_ctx() -> MagicMock:
        return mock_ctx

    import core.db

    original = core.db.get_conn_ctx
    core.db.get_conn_ctx = mock_get_conn_ctx  # type: ignore[assignment]

    try:
        from plugins.grillo.grillo_impl import GrilloPlugin

        result = await GrilloPlugin.create_activity_log(
            beat_type="test_beat", prompt_text="Test prompt"
        )

        assert result == 999
    finally:
        core.db.get_conn_ctx = original


@pytest.mark.asyncio
async def test_grillo_outreach_uses_last_active_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that outreach uses the last active interface instead of random."""
    from plugins.grillo.grillo_outreach import GrilloOutreachPlugin
    import core.recent_chats as recent_chats

    # Mock get_last_active_chats to return a specific chat
    async def mock_get_last_active_chats(n: int = 10) -> list:
        return [123456, 789012]

    # Mock get_chat_path to return telegram interface
    def mock_get_chat_path(chat_id: int) -> str:
        if chat_id == 123456:
            return "telegram_bot/123456"
        return "discord_bot/789012"

    monkeypatch.setattr(
        recent_chats, "get_last_active_chats", mock_get_last_active_chats
    )
    monkeypatch.setattr(recent_chats, "get_chat_path", mock_get_chat_path)

    plugin = GrilloOutreachPlugin()
    # Set allowed interfaces
    plugin.target_interfaces = "telegram_bot,discord_bot"

    interface, chat_id = await plugin._get_target_interface_and_chat()

    # Should use the first matching recent chat (telegram)
    assert interface == "telegram_bot"
    assert chat_id == "123456"


@pytest.mark.asyncio
async def test_grillo_outreach_fallback_when_no_recent_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that outreach falls back when no recent chats match allowed interfaces."""
    from plugins.grillo.grillo_outreach import GrilloOutreachPlugin
    import core.recent_chats as recent_chats

    # Mock get_last_active_chats to return chats with unknown interfaces
    async def mock_get_last_active_chats(n: int = 10) -> list:
        return [123456]

    def mock_get_chat_path(chat_id: int) -> str:
        return "unknown_interface/123456"

    monkeypatch.setattr(
        recent_chats, "get_last_active_chats", mock_get_last_active_chats
    )
    monkeypatch.setattr(recent_chats, "get_chat_path", mock_get_chat_path)

    plugin = GrilloOutreachPlugin()
    plugin.target_interfaces = "telegram_bot"
    plugin.target_chat_ids = "999888"

    interface, chat_id = await plugin._get_target_interface_and_chat()

    # Should fall back to first configured interface and chat
    assert interface == "telegram_bot"
    assert chat_id == "999888"
