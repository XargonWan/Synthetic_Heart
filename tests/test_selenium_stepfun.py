import sys
from pathlib import Path
import unittest

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Import plugin module dynamically (avoid depending on package import machinery during test collection)
import importlib.util

spec = importlib.util.spec_from_file_location(
    "cortex.selenium_engine.dev.selenium_stepfun",
    Path(__file__).resolve().parents[1]
    / "cortex"
    / "selenium_engine"
    / "dev"
    / "selenium_stepfun.py",
)
selenium_stepfun = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selenium_stepfun)
SeleniumStepFunPlugin = getattr(selenium_stepfun, "PLUGIN_CLASS")


class TestSeleniumStepFunEngine(unittest.TestCase):
    def test_stepfun_has_unlogged_limit(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        with open(
            repo_root / "cortex" / "selenium_engine" / "dev" / "selenium_stepfun.py",
            "r",
        ) as f:
            content = f.read()
        # Ensure the strict unlogged prompt limit required by StepFun's free UI
        self.assertIn('"unlogged": 1000', content)

    def test_stepfun_selectors_present(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        with open(
            repo_root / "cortex" / "selenium_engine" / "dev" / "selenium_stepfun.py",
            "r",
        ) as f:
            content = f.read()
        # Confirm the primary prompt textarea selector (exact from user)
        self.assertIn("Publisher_textarea__pMX9t", content)
        # Confirm popup-close selector exists (radix popup)
        self.assertIn("#radix-«r5» > div > button", content)

    def test_dynamic_promotion_is_noop_and_send_button_order_is_static(self):
        plugin = SeleniumStepFunPlugin()

        # Dynamic promotion must be NO-OP for this engine
        kind = "prompt_area"
        original = plugin.selectors[kind].copy()
        test_selector = original[-1]
        plugin._on_selector_success(kind, test_selector)
        # order must remain unchanged
        self.assertEqual(plugin.selectors[kind], original)

        # Confirm send_button selectors are statically promoted as requested
        send_list = plugin.selectors["send_button"]
        self.assertTrue(send_list[0].startswith("#«rc»"))
        self.assertIn("button.inline-flex.items-center.justify-center", send_list[1])


if __name__ == "__main__":
    unittest.main()
