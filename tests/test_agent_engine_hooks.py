import asyncio
import pytest

from core.agent_core import AgentCore


class HookEngine:
    def __init__(self):
        self.agent_attached = False
        self.agent_ref = None

    def attach_agent(self, plugin):
        self.agent_attached = True
        self.agent_ref = plugin

    def detach_agent(self, plugin):
        self.agent_attached = False
        self.agent_ref = None

    def supports_agent(self):
        return True

    def agent_execute(self, action_dict, context=None):
        # Simple echo executor
        return {"status": "ok", "action": action_dict}


@pytest.mark.asyncio
async def test_engine_hooks_attach_and_agent_execute(monkeypatch):
    hook = HookEngine()

    class FakeRegistry:
        def get_engine(self, name):
            return hook

        def load_engine(self, name):
            return hook

    monkeypatch.setattr(
        "core.config.get_active_llm", lambda: asyncio.sleep(0, result="some_engine")
    )
    monkeypatch.setattr("core.llm_registry.get_llm_registry", lambda: FakeRegistry())

    agent = AgentCore()
    agent._enabled = True
    await agent.attach_to_active_engine()

    assert hook.agent_attached is True
    assert hook.agent_ref is agent

    # Test agent_execute usage
    out = hook.agent_execute({"type": "dummy", "payload": {"x": 1}})
    assert out["status"] == "ok"
    assert out["action"]["payload"]["x"] == 1

    await agent.detach_from_engine()
    assert hook.agent_attached is False
