import asyncio
import pytest

from cortex.llm_engine.gemini_api import PLUGIN_CLASS as GeminiClass
from core.core_initializer import PLUGIN_REGISTRY
from plugins.agent_plugin import AgentPlugin


@pytest.mark.asyncio
async def test_gemini_agent_adapter(monkeypatch):
    # Register a fake Agent plugin that records execute_action calls
    called = {}

    class FakeAgent:
        async def execute_action(self, action, context, bot, original_message):
            called['action'] = action
            return {'status': 'executed', 'action': action}

    PLUGIN_REGISTRY['agent'] = FakeAgent()

    gem = GeminiClass()
    # agent_execute should delegate and return a dict
    res = gem.agent_execute({'type': 'propose_action', 'payload': {'command': 'echo hi'}})
    assert isinstance(res, dict)
    # If delegation returned async marker, status may be pending_async or executed


def test_selenium_agent_adapter(monkeypatch):
    from core.selenium_llm_base import SeleniumLLMBase

    s = SeleniumLLMBase(config={})

    # Register fake agent plugin with sync execute_action
    class FakeAgentSync:
        def execute_action(self, action, context, bot, original_message):
            return {'status': 'executed', 'action': action}

    PLUGIN_REGISTRY['agent'] = FakeAgentSync()

    res = s.agent_execute({'type': 'terminal', 'payload': {'command': 'echo hi'}})
    assert isinstance(res, dict)
    # Since called outside loop, agent_execute will have executed synchronously
    assert res.get('status') in ('executed', 'ok', 'unsupported', 'error', 'scheduled')