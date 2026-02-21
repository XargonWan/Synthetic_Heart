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

    async def test_scope_commands_registered(self):
        """New cortex scope helpers should appear in the command list."""
        cmds = list_commands()
        self.assertIn("cortex_live", cmds)
        self.assertIn("cortex_grillo", cmds)
        self.assertIn("cortex_trainer", cmds)

    async def test_handle_scope_command_message(self):
        """Generic handler should recognise the new commands (returning a string)."""
        # patch underlying handler to avoid side effects
        with patch("core.command_registry.cortex_live_alias", new=AsyncMock(return_value="ok")):
            res = await handle_command_message("/cortex_live manual")
            self.assertEqual(res, "ok")


if __name__ == "__main__":
    unittest.main()
