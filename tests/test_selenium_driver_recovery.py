import asyncio

from cortex.selenium_engine.selenium_llm_base import SeleniumLLMBase, FrozenDriverError
from core.config_manager import config_registry


def test_selenium_driver_recovery(monkeypatch):
    s = SeleniumLLMBase()
    s._initialized = True

    calls = {"count": 0}

    def fake_execute_complete_workflow(prompt_text, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            # Simulate a frozen driver on first attempt
            raise FrozenDriverError("Simulated freeze")
        return "OK"

    monkeypatch.setattr(s, "_execute_complete_workflow", fake_execute_complete_workflow)

    # Ensure we try at least one recovery
    asyncio.run(config_registry.set_value("SELENIUM_DRIVER_RECOVERY_RETRIES", 1))

    resp = asyncio.run(s.generate_response("hello"))
    assert resp == "OK"
    assert calls["count"] >= 2
