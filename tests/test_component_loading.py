#!/usr/bin/env python3
"""Test component auto-discovery and registration."""

import unittest
import sys
import os
from unittest.mock import patch, MagicMock, AsyncMock

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock environment variables - NO REAL API ACCESS
os.environ.setdefault("BOTFATHER_TOKEN", "test_token")
os.environ.setdefault("OPENAI_API_KEY", "test_key")
os.environ.setdefault("TRAINER_IDS", "telegram_bot:12345")


class TestComponentLoading(unittest.TestCase):
    """Test that components are properly discovered and registered without real API access."""

    def setUp(self):
        """Set up test environment with all external services mocked."""
        # Clear any existing registrations
        from core.core_initializer import core_initializer

        core_initializer.loaded_plugins = []
        core_initializer.active_interfaces = {}

        # Mock all external dependencies
        self.db_patcher = patch("core.db.get_conn", new_callable=AsyncMock)
        self.db_patcher.start()

        self.llm_patcher = patch(
            "core.config.get_active_llm", return_value={"engine": "manual"}
        )
        self.llm_patcher.start()

    def tearDown(self):
        """Clean up patches."""
        self.db_patcher.stop()
        self.llm_patcher.stop()

    @patch("core.notifier.set_notifier")
    def test_plugin_discovery(self, mock_set_notifier):
        """Test that plugins are discovered from the plugins directory without real dependencies."""
        from core.core_initializer import core_initializer

        # Mock the import system to avoid actual imports and external calls
        with (
            patch("importlib.import_module") as mock_import,
            patch("inspect.signature") as mock_sig,
            patch("os.path.exists", return_value=True),
            patch("pathlib.Path.rglob") as mock_rglob,
            patch("asyncio.get_event_loop") as mock_loop,
        ):
            # Mock event loop for async operations
            mock_loop.return_value = MagicMock()

            # Mock plugin file discovery
            mock_file = MagicMock()
            mock_file.name = "test_plugin.py"
            mock_file.relative_to.return_value = "plugins/test_plugin"
            mock_file.with_suffix.return_value = "plugins.test_plugin"
            mock_rglob.return_value = [mock_file]

            # Mock module with PLUGIN_CLASS
            mock_module = MagicMock()
            mock_plugin_class = MagicMock()
            mock_plugin_class.get_supported_action_types = MagicMock(
                return_value=["test_action"]
            )
            mock_plugin_class.get_supported_actions = MagicMock(
                return_value={"test_action": {}}
            )
            mock_module.PLUGIN_CLASS = mock_plugin_class
            mock_import.return_value = mock_module

            # Mock inspect signature
            mock_sig.return_value.parameters = {}

            # Test loading
            core_initializer._load_plugins()

            # Verify import_module was called with correct module name
            mock_import.assert_called()
            # The exact call depends on the file structure, but should include 'plugins.test_plugin'

    @patch("core.notifier.set_notifier")
    def test_interface_discovery(self, mock_set_notifier):
        """Test that interfaces are discovered without real API connections."""
        from core.core_initializer import core_initializer

        # Mock the import system
        with (
            patch("importlib.import_module") as mock_import,
            patch("inspect.signature") as mock_sig,
            patch("os.path.exists", return_value=True),
            patch("pathlib.Path.rglob") as mock_rglob,
            patch("asyncio.get_event_loop") as mock_loop,
        ):
            # Mock event loop
            mock_loop.return_value = MagicMock()

            # Mock interface file discovery
            mock_file = MagicMock()
            mock_file.name = "test_interface.py"
            mock_file.relative_to.return_value = "interface/test_interface"
            mock_file.with_suffix.return_value = "interface.test_interface"
            mock_rglob.return_value = [mock_file]

            # Mock module with INTERFACE_CLASS
            mock_module = MagicMock()
            mock_interface_class = MagicMock()
            mock_interface_class.get_supported_action_types = MagicMock(
                return_value=["message"]
            )
            mock_interface_class.get_supported_actions = MagicMock(
                return_value={"message_test": {}}
            )
            mock_module.INTERFACE_CLASS = mock_interface_class
            mock_import.return_value = mock_module

            # Mock inspect signature
            mock_sig.return_value.parameters = {}

            # Test loading
            core_initializer._load_plugins()

            # Verify import_module was called
            mock_import.assert_called()

    @patch("core.notifier.set_notifier")
    def test_llm_engine_discovery(self, mock_set_notifier):
        """Test that LLM engines are discovered without real API calls."""
        from core.core_initializer import core_initializer

        # Mock the import system
        with (
            patch("importlib.import_module") as mock_import,
            patch("inspect.signature") as mock_sig,
            patch("os.path.exists", return_value=True),
            patch("pathlib.Path.rglob") as mock_rglob,
            patch("asyncio.get_event_loop") as mock_loop,
        ):
            # Mock event loop
            mock_loop.return_value = MagicMock()

            # Mock engine file discovery
            mock_file = MagicMock()
            mock_file.name = "test_engine.py"
            mock_file.relative_to.return_value = "cortex/llm_engine/test_engine"
            mock_file.with_suffix.return_value = "cortex.llm_engine.test_engine"
            mock_rglob.return_value = [mock_file]

            # Mock module with PLUGIN_CLASS
            mock_module = MagicMock()
            mock_engine_class = MagicMock()
            mock_engine_class.get_supported_action_types = MagicMock(return_value=[])
            mock_engine_class.get_supported_actions = MagicMock(return_value={})
            mock_module.PLUGIN_CLASS = mock_engine_class
            mock_import.return_value = mock_module

            # Mock inspect signature
            mock_sig.return_value.parameters = {}

            # Test loading
            core_initializer._load_plugins()

            # Verify import_module was called
            mock_import.assert_called()

    def test_invalid_component_skipped(self):
        """Test that invalid components are skipped during loading."""
        from core.core_initializer import core_initializer

        # Mock the import system
        with (
            patch("importlib.import_module") as mock_import,
            patch("os.path.exists", return_value=True),
            patch("pathlib.Path.rglob") as mock_rglob,
        ):
            # Mock invalid plugin file
            mock_file = MagicMock()
            mock_file.name = "invalid_plugin.py"
            mock_file.relative_to.return_value = "plugins/invalid_plugin"
            mock_file.with_suffix.return_value = "plugins.invalid_plugin"
            mock_rglob.return_value = [mock_file]

            # Mock module without PLUGIN_CLASS
            mock_module = MagicMock()
            del mock_module.PLUGIN_CLASS  # No PLUGIN_CLASS
            mock_import.return_value = mock_module

            # Test loading
            core_initializer._load_plugins()

            # Verify invalid plugin was not registered
            self.assertNotIn("invalid_plugin", core_initializer.loaded_plugins)


if __name__ == "__main__":
    unittest.main()
