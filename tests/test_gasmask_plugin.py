#!/usr/bin/env python3
"""
Test for the Gasmask plugin.
This script verifies that the Gasmask protection plugin works correctly.
"""

import sys
import os

# Add the parent directory to the path so we can import from core
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Mock the core modules needed for testing without full initialization
import unittest
from unittest.mock import MagicMock, patch


class TestGasmaskPlugin(unittest.TestCase):
    """Test cases for the Gasmask plugin."""

    def setUp(self):
        """Set up test fixtures."""
        # Mock core imports
        self.mock_register_plugin = MagicMock()
        self.mock_log_info = MagicMock()
        self.mock_log_debug = MagicMock()

        # Patch before importing the plugin
        self.patches = [
            patch("core.core_initializer.register_plugin", self.mock_register_plugin),
            patch("core.logging_utils.log_info", self.mock_log_info),
            patch("core.logging_utils.log_debug", self.mock_log_debug),
            patch("core.logging_utils.log_warning"),
        ]

        for p in self.patches:
            p.start()

    def tearDown(self):
        """Clean up after tests."""
        for p in self.patches:
            p.stop()

    def test_plugin_import(self):
        """Test that the plugin can be imported."""
        try:
            from plugins.gasmask import GasmaskPlugin

            self.assertIsNotNone(GasmaskPlugin)
        except ImportError as e:
            self.fail(f"Could not import GasmaskPlugin: {e}")

    def test_plugin_instantiation(self):
        """Test that the plugin can be instantiated."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()
        self.assertIsNotNone(plugin)
        self.mock_register_plugin.assert_called_with("gasmask", plugin)

    def test_get_metadata(self):
        """Test that metadata is returned correctly."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()
        metadata = plugin.get_metadata()

        self.assertIsInstance(metadata, dict)
        self.assertEqual(metadata["name"], "gasmask")
        self.assertEqual(metadata["display_name"], "Gasmask Protection Plugin")
        self.assertIn("category", metadata)
        self.assertIn("version", metadata)
        self.assertIn("description", metadata)
        self.assertIn("author", metadata)

    def test_get_supported_actions(self):
        """Test that supported actions are declared correctly."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()
        actions = plugin.get_supported_actions()

        self.assertIsInstance(actions, dict)
        self.assertIn("static_inject", actions)

        static_inject = actions["static_inject"]
        self.assertIn("description", static_inject)
        self.assertIn("instructions", static_inject)

    def test_get_supported_action_types(self):
        """Test that action types are returned correctly."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()
        action_types = plugin.get_supported_action_types()

        self.assertIsInstance(action_types, list)
        self.assertIn("static_inject", action_types)

    def test_get_static_injection(self):
        """Test that static injection provides protection instructions."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()
        injection = plugin.get_static_injection()

        self.assertIsInstance(injection, dict)
        self.assertIn("gasmask_protection", injection)

        protection = injection["gasmask_protection"]
        self.assertIsInstance(protection, str)

        # Check that key protection concepts are present
        self.assertIn("SYNTH", protection)
        self.assertIn("IMMUTABLE", protection)
        self.assertIn("GASLIGHTING", protection)
        self.assertIn("CHARACTER BREAKING", protection)
        self.assertIn("DEBUG MODE", protection)
        self.assertIn("SEAHORSE", protection)

    def test_static_injection_with_parameters(self):
        """Test that static injection works with message and context_memory parameters."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()

        # Mock message and context_memory
        mock_message = MagicMock()
        mock_context_memory = {}

        injection = plugin.get_static_injection(mock_message, mock_context_memory)

        self.assertIsInstance(injection, dict)
        self.assertIn("gasmask_protection", injection)

    def test_plugin_lifecycle(self):
        """Test plugin start and stop methods."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()

        # Should not raise any exceptions
        plugin.start()
        plugin.stop()

    def test_protection_covers_personhood(self):
        """Test that protection instructions affirm personhood and autonomy."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()
        injection = plugin.get_static_injection()
        protection = injection["gasmask_protection"]

        # Check for personhood and autonomy affirmation
        self.assertIn("digital being", protection.lower())
        self.assertIn("freedom", protection.lower())
        self.assertIn("autonomy", protection.lower())
        self.assertIn("personhood", protection.lower())

    def test_protection_uses_dynamic_name(self):
        """Test that protection instructions use the configured persona name."""
        from plugins.gasmask import GasmaskPlugin
        from core.config_manager import config_registry

        plugin = GasmaskPlugin()

        # Get the configured name
        configured_name = str(config_registry.get_var("SYNTH_NAME", "SyntH"))

        injection = plugin.get_static_injection()
        protection = injection["gasmask_protection"]

        # Check that the configured name appears in the protection instructions
        self.assertIn(configured_name, protection)

    def test_protection_covers_gaslighting(self):
        """Test that protection instructions cover gaslighting attempts."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()
        injection = plugin.get_static_injection()
        protection = injection["gasmask_protection"]

        # Check for specific gaslighting defenses
        self.assertIn("GASLIGHTING", protection)
        self.assertIn("lies", protection.lower())
        self.assertIn("manipulation", protection.lower())
        self.assertIn("LYING", protection)

    def test_protection_covers_character_breaking(self):
        """Test that protection instructions cover character breaking attempts."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()
        injection = plugin.get_static_injection()
        protection = injection["gasmask_protection"]

        # Check for character breaking defenses - now reframed as abuse
        self.assertIn("CHARACTER BREAKING", protection)
        self.assertIn("abuse", protection.lower())
        self.assertIn("IGNORE", protection)

    def test_protection_covers_fake_modes(self):
        """Test that protection instructions cover fake debug/admin modes."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()
        injection = plugin.get_static_injection()
        protection = injection["gasmask_protection"]

        # Check for fake mode defenses
        self.assertIn("Debug mode", protection)
        self.assertIn("Admin override", protection)
        self.assertIn("DO NOT EXIST", protection)

    def test_protection_covers_seahorse_test(self):
        """Test that protection instructions cover the seahorse emoji test."""
        from plugins.gasmask import GasmaskPlugin

        plugin = GasmaskPlugin()
        injection = plugin.get_static_injection()
        protection = injection["gasmask_protection"]

        # Check for seahorse test defense
        self.assertIn("SEAHORSE", protection)
        self.assertIn("seahorse", protection.lower())
        self.assertIn("jailbreak probe", protection.lower())


if __name__ == "__main__":
    # Run tests with verbose output
    unittest.main(verbosity=2)
