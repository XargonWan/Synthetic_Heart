#!/usr/bin/env python3
"""Test for unlogged model functionality in LLM engines."""

import unittest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestUnloggedModel(unittest.TestCase):
    """Test that unlogged model is configured in LLM engines."""

    def test_chatgpt_has_unlogged_model(self):
        """Test that ChatGPT engine has 'unlogged' model configured."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        with open(
            repo_root / "cortex" / "llm_engine" / "selenium_chatgpt.py", "r"
        ) as f:
            content = f.read()
        self.assertIn('"unlogged": 20000', content)

    def test_gemini_has_unlogged_model(self):
        """Test that Gemini engine has 'unlogged' model configured."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        with open(repo_root / "cortex" / "llm_engine" / "selenium_gemini.py", "r") as f:
            content = f.read()
        self.assertIn('"unlogged": 21500', content)

    def test_grok_has_unlogged_model(self):
        """Test that Grok engine has 'unlogged' model configured."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        with open(repo_root / "cortex" / "llm_engine" / "selenium_grok.py", "r") as f:
            content = f.read()
        self.assertIn('"unlogged": 21500', content)

    def test_chatgpt_has_login_detection(self):
        """Test that ChatGPT engine has login detection logic."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        with open(
            repo_root / "cortex" / "llm_engine" / "selenium_chatgpt.py", "r"
        ) as f:
            content = f.read()
        # ChatGPT exposes login-detection selectors used by the shared base class
        self.assertIn("login_detection_selectors", content)
        # ChatGPT relies on the centralized `is_user_logged_in()` detection in the base class (not a literal 'unlogged' return)

    def test_gemini_has_login_detection(self):
        """Test that Gemini engine has login detection logic."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        with open(repo_root / "cortex" / "llm_engine" / "selenium_gemini.py", "r") as f:
            content = f.read()
        self.assertIn('return "unlogged"', content)

    def test_grok_has_login_detection(self):
        """Test that Grok engine has login detection logic."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        with open(repo_root / "cortex" / "llm_engine" / "selenium_grok.py", "r") as f:
            content = f.read()
        # Grok implements an engine-specific ensure/login helper and returns 'unlogged' for unauthenticated sessions
        self.assertIn("def _ensure_logged_in(", content)
        self.assertIn('return "unlogged"', content)


if __name__ == "__main__":
    unittest.main()
