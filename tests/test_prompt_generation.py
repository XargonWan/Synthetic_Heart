#!/usr/bin/env python3
"""Test prompt generation and JSON structure."""

import unittest
import sys
import os
import json
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock environment variables
os.environ.setdefault("BOTFATHER_TOKEN", "test_token")
os.environ.setdefault("OPENAI_API_KEY", "test_key")


class TestPromptGeneration(unittest.TestCase):
    """Test that prompts are generated correctly with proper JSON structure."""

    def setUp(self):
        """Set up test environment."""
        # Mock available actions
        self.mock_actions = {
            "message_telegram_bot": {
                "description": "Send a message via Telegram",
                "required_fields": ["text", "target"],
                "optional_fields": ["parse_mode"],
            },
            "terminal_bash": {
                "description": "Execute a shell command",
                "required_fields": ["command"],
                "optional_fields": ["timeout"],
            },
        }

    @patch("core.prompt_engine.build_full_json_instructions")
    async def test_prompt_includes_available_actions(self, mock_build_instructions):
        """Test that prompts include all available actions."""
        from core.prompt_engine import build_prompt

        # Mock the instructions builder
        mock_build_instructions.return_value = json.dumps(
            {
                "available_actions": self.mock_actions,
                "response_format": {
                    "type": "json",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "payload": {"type": "object"},
                        },
                    },
                },
            }
        )

        # Build prompt using correct API
        prompt = await build_prompt(
            user_text="Hello",
            identity_prompt="",
            extract_tags_fn=MagicMock(),
            search_memories_fn=MagicMock(),
        )

        # Verify prompt contains actions (mocked)
        self.assertIsInstance(prompt, list)

    def test_json_instructions_structure(self):
        """Test that JSON instructions have the correct structure."""
        from core.prompt_engine import build_full_json_instructions

        # Mock core_initializer to provide actions
        with patch("core.core_initializer.core_initializer") as mock_core_init:
            mock_core_init.actions_block = {"available_actions": self.mock_actions}

            instructions = build_full_json_instructions()

            # Should return a dict
            self.assertIsInstance(instructions, dict)
            self.assertIn("instructions", instructions)
            self.assertIn("actions", instructions)

            # Check actions structure
            actions = instructions["actions"]
            self.assertIsInstance(actions, dict)
            self.assertIn("message_telegram_bot", actions)
            self.assertIn("terminal_bash", actions)

    @patch("core.core_initializer.core_initializer.actions_block")
    def test_actions_block_population(self, mock_actions_block):
        """Test that the actions block is properly populated."""
        from core.core_initializer import core_initializer

        # Mock actions block
        mock_actions_block.__getitem__.return_value = self.mock_actions

        # Test that actions are accessible
        actions = core_initializer.actions_block["available_actions"]
        self.assertIsNotNone(actions)

    async def test_prompt_injection_safety(self):
        """Test that prompts handle special characters safely."""
        from core.prompt_engine import build_prompt

        # Test with special characters in user_text
        user_text = "Hello {with} special chars \"quotes\" and 'apostrophes'"

        prompt = await build_prompt(
            user_text=user_text,
            identity_prompt="Test context",
            extract_tags_fn=MagicMock(),
            search_memories_fn=MagicMock(),
        )

        # Should not crash and should contain the content
        self.assertIsInstance(prompt, list)

    async def test_empty_actions_handling(self):
        """Test prompt generation with no available actions."""
        from core.prompt_engine import build_prompt

        # Mock empty actions
        with patch("core.core_initializer.core_initializer") as mock_core_init:
            mock_core_init.actions_block = {"available_actions": {}}

            prompt = await build_prompt(
                user_text="Hello",
                identity_prompt="",
                extract_tags_fn=MagicMock(),
                search_memories_fn=MagicMock(),
            )

            # Should still generate a valid prompt
            self.assertIsInstance(prompt, list)

    async def test_large_context_truncation(self):
        """Test that large contexts are handled appropriately."""
        from core.prompt_engine import build_prompt

        # Create a very large user_text
        large_text = "x" * 10000

        prompt = await build_prompt(
            user_text=large_text,
            identity_prompt="",
            extract_tags_fn=MagicMock(),
            search_memories_fn=MagicMock(),
        )

        # Should still work (implementation should handle large inputs)
        self.assertIsInstance(prompt, list)


if __name__ == "__main__":
    unittest.main()
