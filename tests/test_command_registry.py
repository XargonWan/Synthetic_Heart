import unittest
import sys
import os

# Add parent directory to path so that 'core' can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOTFATHER_TOKEN", "test")

from core.command_registry import execute_command, list_commands, handle_command_message


class TestCommandRegistry(unittest.TestCase):
    async def test_help_command_registered(self):
        self.assertIn("help", list_commands())
        text = await execute_command("help")
        self.assertIn("synth – Available Commands", text)
        self.assertIn("/context", text)

    async def test_unknown_command_returns_none(self):
        """Test that unknown commands return None via handle_command_message instead of raising exceptions."""
        result = await handle_command_message("/unknown_command_that_does_not_exist")
        self.assertIsNone(result)

    async def test_execute_command_raises_for_unknown(self):
        """Test that execute_command still raises exceptions for unknown commands."""
        with self.assertRaises(ValueError):
            await execute_command("unknown_command_that_does_not_exist")


if __name__ == "__main__":
    unittest.main()
