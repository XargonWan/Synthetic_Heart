import unittest
from core.cortex_registry import get_cortex_registry, register_default_engines


class TestCortexRegistryAutoDiscover(unittest.TestCase):
    def test_autodiscover_registers_cortex_engines(self):
        reg = get_cortex_registry()
        # Clear any previous registration for test isolation
        reg._engine_modules.clear()
        reg._engine_meta.clear()

        # Run auto-discovery
        register_default_engines()

        engines = reg.get_available_engines()
        # Basic sanity: llm_provider engines should be discovered
        self.assertTrue(
            any(e in engines for e in ("gemini_api", "openapi", "openrouter")),
            f"Expected at least one llm_provider engine, found: {engines}",
        )

    def test_autodiscover_registers_dev_engines_when_enabled(self):
        reg = get_cortex_registry()
        reg._engine_modules.clear()
        reg._engine_meta.clear()

        register_default_engines(dev_enabled=True)
        engines = reg.get_available_engines()
        # Dev engines (e.g. manual) should be discoverable when dev is enabled
        self.assertTrue(
            len(engines) > 0,
            "Expected at least one engine after dev discovery",
        )


if __name__ == "__main__":
    unittest.main()
