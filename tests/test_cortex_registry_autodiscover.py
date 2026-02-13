import unittest
from core.cortex_registry import get_cortex_registry, register_default_engines


class TestCortexRegistryAutoDiscover(unittest.TestCase):
    def test_autodiscover_registers_cortex_llm_engines(self):
        reg = get_cortex_registry()
        # Clear any previous registration for test isolation
        reg._engine_modules.clear()
        reg._engine_meta.clear()

        # Run auto-discovery
        register_default_engines()

        engines = reg.get_available_engines()
        # Basic sanity: our cortex/llm_engine folder should register known engines
        self.assertIn("selenium_chatgpt", engines)
        self.assertIn("selenium_gemini", engines)
        # The new StepFun engine should also be discoverable under cortex
        self.assertIn("selenium_stepfun", engines)


if __name__ == "__main__":
    unittest.main()
