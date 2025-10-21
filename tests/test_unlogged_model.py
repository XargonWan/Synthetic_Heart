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
        with open("/videodrome/videodrome-deployment/Synthetic_Heart/llm_engines/selenium_chatgpt.py", 'r') as f:
            content = f.read()
        self.assertIn('"unlogged": 1000', content)

    def test_gemini_has_unlogged_model(self):
        """Test that Gemini engine has 'unlogged' model configured."""
        with open("/videodrome/videodrome-deployment/Synthetic_Heart/llm_engines/selenium_gemini.py", 'r') as f:
            content = f.read()
        self.assertIn('"unlogged": 1000', content)

    def test_grok_has_unlogged_model(self):
        """Test that Grok engine has 'unlogged' model configured."""
        with open("/videodrome/videodrome-deployment/Synthetic_Heart/llm_engines/selenium_grok.py", 'r') as f:
            content = f.read()
        self.assertIn('"unlogged": 1000', content)

    def test_chatgpt_has_login_detection(self):
        """Test that ChatGPT engine has login detection logic."""
        with open("/videodrome/videodrome-deployment/Synthetic_Heart/llm_engines/selenium_chatgpt.py", 'r') as f:
            content = f.read()
        self.assertIn('_check_login_status_on_startup', content)
        self.assertIn('login_button_selectors', content)
        self.assertIn('login_texts', content)
        # ChatGPT uses startup login check with base class method instead of returning "unlogged" from get_current_model

    def test_gemini_has_login_detection(self):
        """Test that Gemini engine has login detection logic."""
        with open("/videodrome/videodrome-deployment/Synthetic_Heart/llm_engines/selenium_gemini.py", 'r') as f:
            content = f.read()
        self.assertIn('return "unlogged"', content)

    def test_grok_has_login_detection(self):
        """Test that Grok engine has login detection logic."""
        with open("/videodrome/videodrome-deployment/Synthetic_Heart/llm_engines/selenium_grok.py", 'r') as f:
            content = f.read()
        self.assertIn('def _is_user_logged_in(self)', content)
        self.assertIn('return "unlogged"', content)


if __name__ == '__main__':
    unittest.main()