import pytest

from engines.external_engines.gemini_api import PLUGIN_CLASS as GeminiClass
from core.core_initializer import PLUGIN_REGISTRY


@pytest.mark.asyncio
async def test_gemini_agent_adapter(monkeypatch):
    # Register a fake Agent plugin that records execute_action calls
    called = {}

    class FakeAgent:
        async def execute_action(self, action, context, bot, original_message):
            called["action"] = action
            return {"status": "executed", "action": action}

    PLUGIN_REGISTRY["agent"] = FakeAgent()

    gem = GeminiClass()
    # agent_execute should delegate and return a dict
    res = gem.agent_execute(
        {"type": "propose_action", "payload": {"command": "echo hi"}}
    )
    assert isinstance(res, dict)
    # If delegation returned async marker, status may be pending_async or executed
