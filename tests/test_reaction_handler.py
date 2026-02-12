"""
Test suite for core/reaction_handler.py

Tests the reaction handling functionality when bot is mentioned.
"""

import pytest
import os
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace
from core.reaction_handler import get_reaction_emoji, react_when_mentioned


class TestGetReactionEmoji:
    """Test get_reaction_emoji function."""

    def test_returns_emoji_when_set(self):
        """Test that it returns emoji when REACT_WHEN_MENTIONED is set."""
        with patch.dict(os.environ, {"REACT_WHEN_MENTIONED": "👀"}):
            emoji = get_reaction_emoji()
            assert emoji == "👀"

    def test_returns_none_when_empty(self):
        """Test that it returns None when REACT_WHEN_MENTIONED is empty."""
        with patch.dict(os.environ, {"REACT_WHEN_MENTIONED": ""}, clear=True):
            emoji = get_reaction_emoji()
            assert emoji is None

    def test_returns_none_when_whitespace(self):
        """Test that it returns None when REACT_WHEN_MENTIONED is whitespace."""
        with patch.dict(os.environ, {"REACT_WHEN_MENTIONED": "   "}, clear=True):
            emoji = get_reaction_emoji()
            assert emoji is None

    def test_returns_none_when_not_set(self):
        """Test that it returns None when REACT_WHEN_MENTIONED is not set."""
        with patch.dict(os.environ, {}, clear=True):
            emoji = get_reaction_emoji()
            assert emoji is None


class TestReactWhenMentioned:
    """Test react_when_mentioned function."""

    @pytest.mark.asyncio
    async def test_no_reaction_when_emoji_not_configured(self):
        """Test that no reaction is added when REACT_WHEN_MENTIONED is not set."""
        with patch.dict(os.environ, {}, clear=True):
            bot = MagicMock()
            message = SimpleNamespace(chat_id=123, message_id=456)

            result = await react_when_mentioned(bot, message)

            assert result is False
            assert not bot.set_message_reaction.called

    @pytest.mark.asyncio
    async def test_telegram_reaction_success(self):
        """Test successful reaction addition on Telegram."""
        with patch.dict(os.environ, {"REACT_WHEN_MENTIONED": "👀"}):
            bot = AsyncMock()
            bot.set_message_reaction = AsyncMock()

            # Create message with chat object
            chat = SimpleNamespace(id=123)
            message = SimpleNamespace(chat=chat, chat_id=123, message_id=456)

            result = await react_when_mentioned(bot, message)

            assert result is True
            bot.set_message_reaction.assert_called_once_with(
                chat_id=123, message_id=456, reaction="👀", is_big=False
            )

    @pytest.mark.asyncio
    async def test_telegram_reaction_with_chat_id_from_chat(self):
        """Test reaction when chat_id is obtained from message.chat.id."""
        with patch.dict(os.environ, {"REACT_WHEN_MENTIONED": "🔥"}):
            bot = AsyncMock()
            bot.set_message_reaction = AsyncMock()

            # Message without direct chat_id but with chat.id
            chat = SimpleNamespace(id=789)
            message = SimpleNamespace(chat=chat, message_id=101)

            result = await react_when_mentioned(bot, message)

            assert result is True
            bot.set_message_reaction.assert_called_once_with(
                chat_id=789, message_id=101, reaction="🔥", is_big=False
            )

    @pytest.mark.asyncio
    async def test_no_reaction_when_missing_chat_id(self):
        """Test that no reaction is added when chat_id is missing."""
        with patch.dict(os.environ, {"REACT_WHEN_MENTIONED": "👀"}):
            bot = AsyncMock()
            bot.set_message_reaction = AsyncMock()

            # Message without chat_id
            message = SimpleNamespace(message_id=456)

            result = await react_when_mentioned(bot, message)

            assert result is False
            assert not bot.set_message_reaction.called

    @pytest.mark.asyncio
    async def test_no_reaction_when_missing_message_id(self):
        """Test that no reaction is added when message_id is missing."""
        with patch.dict(os.environ, {"REACT_WHEN_MENTIONED": "👀"}):
            bot = AsyncMock()
            bot.set_message_reaction = AsyncMock()

            # Message without message_id
            chat = SimpleNamespace(id=123)
            message = SimpleNamespace(chat=chat, chat_id=123)

            result = await react_when_mentioned(bot, message)

            assert result is False
            assert not bot.set_message_reaction.called

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self):
        """Test that exceptions are caught and logged."""
        with patch.dict(os.environ, {"REACT_WHEN_MENTIONED": "👀"}):
            bot = AsyncMock()
            bot.set_message_reaction = AsyncMock(side_effect=Exception("API Error"))

            chat = SimpleNamespace(id=123)
            message = SimpleNamespace(chat=chat, chat_id=123, message_id=456)

            result = await react_when_mentioned(bot, message)

            assert result is False
            bot.set_message_reaction.assert_called_once()

    @pytest.mark.asyncio
    async def test_unsupported_interface(self):
        """Test that unsupported interfaces return False."""
        with patch.dict(os.environ, {"REACT_WHEN_MENTIONED": "👀"}):
            # Bot without set_message_reaction method
            bot = MagicMock(spec=[])

            chat = SimpleNamespace(id=123)
            message = SimpleNamespace(chat=chat, chat_id=123, message_id=456)

            result = await react_when_mentioned(bot, message)

            assert result is False
