import asyncio

from plugins.agent_plugin import AgentPlugin


class FakeEngine:
    def __init__(self):
        self.attached = False

    def attach_agent(self, plugin):
        self.attached = True

    def detach_agent(self, plugin):
        self.attached = False


async def test_agent_attaches_to_engine(monkeypatch):
    # Create fake registry with a loaded engine
    from core.cortex_registry import get_cortex_registry

    registry = get_cortex_registry()

    # Ensure no engine with name 'fakeengine' is present
    registry._engines.pop("fakeengine", None)

    # Register fake engine instance
    registry._engines["fakeengine"] = FakeEngine()

    # Patch get_active_llm to return 'fakeengine'
    import core.config as conf

    async def fake_get_active():
        return "fakeengine"

    monkeypatch.setattr(conf, "get_active_cortex_engine", fake_get_active)

    # Instantiate plugin - attach happens async, wait a short tick
    plugin = AgentPlugin(notify_fn=lambda m: None)

    await asyncio.sleep(0.1)

    assert hasattr(plugin, "_attached_engine")
    assert plugin._attached_engine == "fakeengine"
    assert registry.get_engine("fakeengine").attached is True
