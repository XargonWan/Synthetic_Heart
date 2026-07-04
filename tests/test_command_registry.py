import unittest
import sys
import os
from unittest.mock import AsyncMock, patch

# Add parent directory to path so that 'core' can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOTFATHER_TOKEN", "test")

from core.command_registry import execute_command, list_commands, handle_command_message


class TestCommandRegistry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # No engines are registered in the bare test environment; commands
        # that resolve the active Cortex engine need a populated registry.
        self.registry_patcher = patch("core.cortex_registry.get_cortex_registry")
        mock_registry = self.registry_patcher.start().return_value
        mock_registry.get_available_engines.return_value = ["manual"]
        mock_registry.get_default_engine.return_value = "manual"

    def tearDown(self):
        self.registry_patcher.stop()

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
        # The command registry captures the alias function object at import
        # time, so patch the cortex_command it late-binds instead.
        with patch(
            "core.command_registry.cortex_command", new=AsyncMock(return_value="ok")
        ):
            res = await handle_command_message("/cortex_live manual")
            self.assertEqual(res, "ok")


if __name__ == "__main__":
    unittest.main()
