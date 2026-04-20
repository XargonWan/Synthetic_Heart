"""
Test suite for core/reaction_handler.py

Tests the reaction handling functionality when bot is mentioned.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.reaction_handler import get_reaction_emoji, react_when_mentioned


class TestGetReactionEmoji:
    """Test get_reaction_emoji function."""

    def test_returns_emoji_when_set(self):
        """Test that it returns emoji when REACT_WHEN_MENTIONED is set."""
        import core.reaction_handler as rh

        rh.REACT_WHEN_MENTIONED = "👀"  # type: ignore[assignment]
        emoji = get_reaction_emoji()
        assert emoji == "👀"

    def test_returns_none_when_empty(self):
        """Test that it returns None when REACT_WHEN_MENTIONED is empty."""
        import core.reaction_handler as rh

        rh.REACT_WHEN_MENTIONED = ""  # type: ignore[assignment]
        emoji = get_reaction_emoji()
        assert emoji is None

    def test_returns_none_when_whitespace(self):
        """Test that it returns None when REACT_WHEN_MENTIONED is whitespace."""
        import core.reaction_handler as rh

        rh.REACT_WHEN_MENTIONED = "   "  # type: ignore[assignment]
        emoji = get_reaction_emoji()
        assert emoji is None

    def test_returns_none_when_not_set(self):
        """Test that it returns None when REACT_WHEN_MENTIONED is not set."""
        import core.reaction_handler as rh

        rh.REACT_WHEN_MENTIONED = ""  # type: ignore[assignment]
        emoji = get_reaction_emoji()
        assert emoji is None


class TestReactWhenMentioned:
    """Test react_when_mentioned function."""

    @pytest.mark.asyncio
    async def test_no_reaction_when_emoji_not_configured(self):
        """Test that no reaction is added when REACT_WHEN_MENTIONED is not set."""
        import core.reaction_handler as rh

        rh.REACT_WHEN_MENTIONED = ""  # type: ignore[assignment]
        bot = MagicMock()
        message = SimpleNamespace(chat_id=123, message_id=456)

        result = await react_when_mentioned(bot, message, str(rh.REACT_WHEN_MENTIONED))
        assert result is False
        assert not bot.set_message_reaction.called

    @pytest.mark.asyncio
    async def test_telegram_reaction_success(self):
        """Test successful reaction addition on Telegram."""
        import core.reaction_handler as rh

        rh.REACT_WHEN_MENTIONED = "👀"  # type: ignore[assignment]
        bot = AsyncMock()
        bot.add_reaction = AsyncMock(return_value=True)

        # Create message with chat object (passed through unchanged)
        chat = SimpleNamespace(id=123)
        message = SimpleNamespace(chat=chat, chat_id=123, message_id=456)

        result = await react_when_mentioned(bot, message, str(rh.REACT_WHEN_MENTIONED))
        assert result is True
        bot.add_reaction.assert_called_once_with(message, "👀")

    @pytest.mark.asyncio
    async def test_add_reaction_called_with_message_and_emoji(self):
        """Ensure react_when_mentioned forwards the message and emoji to add_reaction."""
        import core.reaction_handler as rh

        rh.REACT_WHEN_MENTIONED = "🔥"  # type: ignore[assignment]
        bot = AsyncMock()
        bot.add_reaction = AsyncMock(return_value=True)

        # Message object can be arbitrary; react_when_mentioned doesn't inspect it
        chat = SimpleNamespace(id=789)
        message = SimpleNamespace(chat=chat, message_id=101)

        result = await react_when_mentioned(bot, message, str(rh.REACT_WHEN_MENTIONED))
        assert result is True
        bot.add_reaction.assert_called_once_with(message, "🔥")

    @pytest.mark.asyncio
    async def test_reaction_returns_bot_response_even_if_message_attrs_missing(self):
        """verify that missing chat/message attributes don't crash react_when_mentioned.

        Since the function simply hands the message object to `add_reaction`, the
        behaviour depends entirely on the interface implementation. Here we
        simulate a bot that returns False when it can't handle the message.
        """
        import core.reaction_handler as rh

        rh.REACT_WHEN_MENTIONED = "👀"  # type: ignore[assignment]
        bot = AsyncMock()
        bot.add_reaction = AsyncMock(return_value=False)

        # Message lacking chat_id or chat
        message = SimpleNamespace(message_id=456)

        result = await react_when_mentioned(bot, message, str(rh.REACT_WHEN_MENTIONED))
        assert result is False
        bot.add_reaction.assert_called_once_with(message, "👀")

    @pytest.mark.asyncio
    async def test_reaction_propagates_exception_from_interface(self):
        """Simulate an interface raising an exception and ensure it's caught."""
        import core.reaction_handler as rh

        rh.REACT_WHEN_MENTIONED = "👀"  # type: ignore[assignment]
        bot = AsyncMock()
        bot.add_reaction = AsyncMock(side_effect=Exception("API Error"))

        chat = SimpleNamespace(id=123)
        message = SimpleNamespace(chat=chat, chat_id=123, message_id=456)

        result = await react_when_mentioned(bot, message, str(rh.REACT_WHEN_MENTIONED))
        assert result is False
        bot.add_reaction.assert_called_once_with(message, "👀")

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self):
        """Test that exceptions propagated from add_reaction are handled."""
        import core.reaction_handler as rh

        rh.REACT_WHEN_MENTIONED = "👀"  # type: ignore[assignment]
        bot = AsyncMock()
        bot.add_reaction = AsyncMock(side_effect=Exception("API Error"))

        chat = SimpleNamespace(id=123)
        message = SimpleNamespace(chat=chat, chat_id=123, message_id=456)

        result = await react_when_mentioned(bot, message, str(rh.REACT_WHEN_MENTIONED))
        assert result is False
        bot.add_reaction.assert_called_once_with(message, "👀")

    @pytest.mark.asyncio
    async def test_unsupported_interface(self):
        """Test that unsupported interfaces return False."""
        import core.reaction_handler as rh

        rh.REACT_WHEN_MENTIONED = "👀"  # type: ignore[assignment]
        # Bot without add_reaction method
        bot = MagicMock(spec=[])

        chat = SimpleNamespace(id=123)
        message = SimpleNamespace(chat=chat, chat_id=123, message_id=456)

        result = await react_when_mentioned(bot, message, str(rh.REACT_WHEN_MENTIONED))
        assert result is False
        assert not hasattr(bot, "add_reaction")
