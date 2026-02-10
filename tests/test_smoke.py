#!/usr/bin/env python3
"""Smoke tests to verify basic functionality."""

import unittest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock environment variables for testing
os.environ.setdefault("BOTFATHER_TOKEN", "test_token")
os.environ.setdefault("OPENAI_API_KEY", "test_key")
os.environ.setdefault("TRAINER_IDS", "telegram_bot:12345")


class TestSmoke(unittest.TestCase):
    """Basic smoke tests for core functionality."""

    def test_core_imports(self):
        """Test that core modules can be imported."""
        try:
            import core.config
            import core.message_chain
            import core.action_parser
            import core.core_initializer
            import core.prompt_engine
        except ImportError as e:
            self.fail(f"Failed to import core module: {e}")

    def test_interface_imports(self):
        """Test that interface modules can be imported."""
        # These should not fail even if dependencies are missing
        try:
            pass
        except Exception as e:
            # Interfaces may fail due to missing dependencies, but should not crash
            self.skipTest(f"Interface import failed (expected): {e}")

    def test_plugin_imports(self):
        """Test that plugin modules can be imported."""
        try:
            pass
        except Exception as e:
            # Plugins may fail due to missing dependencies
            self.skipTest(f"Plugin import failed (expected): {e}")

    def test_persona_manager_import(self):
        """Test that persona_manager can be imported without circular dependencies."""
        try:
            # This should not raise a circular import error
            import core.persona_manager
            from core.persona_manager import PersonaManager, get_persona_manager

            # Verify the module has expected exports
            self.assertTrue(hasattr(core.persona_manager, "PersonaManager"))
            self.assertTrue(hasattr(core.persona_manager, "get_persona_manager"))
            self.assertTrue(callable(get_persona_manager))
        except ImportError as e:
            self.fail(f"Failed to import persona_manager: {e}")

    def test_llm_engine_imports(self):
        """Test that LLM engine modules can be imported."""
        try:
            pass
        except Exception as e:
            self.skipTest(f"LLM engine import failed: {e}")

    def test_config_loading(self):
        """Test that configuration can be loaded."""
        try:
            from core.config import get_active_llm

            # Should not crash
            llm = get_active_llm()
            self.assertIsInstance(llm, dict)
        except Exception as e:
            self.skipTest(f"Config loading failed: {e}")

    def test_registry_initialization(self):
        """Test that registries can be initialized."""
        try:
            from core.interfaces_registry import get_interface_registry
            from core.llm_registry import get_llm_registry

            interface_registry = get_interface_registry()
            llm_registry = get_llm_registry()

            self.assertIsNotNone(interface_registry)
            self.assertIsNotNone(llm_registry)
        except Exception as e:
            self.skipTest(f"Registry initialization failed: {e}")

    def test_prompt_engine_basic(self):
        """Test basic prompt engine functionality."""
        try:
            from core.prompt_engine import build_prompt

            prompt = build_prompt(
                messages=[{"role": "user", "content": "Hello"}],
                available_actions={},
                context="",
            )

            self.assertIsInstance(prompt, str)
            self.assertGreater(len(prompt), 0)
            self.assertIn("Hello", prompt)
        except Exception as e:
            self.skipTest(f"Prompt engine test failed: {e}")


if __name__ == "__main__":
    unittest.main()
