from collections import deque
import pytest

from plugins.recon_language_evaluator import ReconLanguageEvaluatorPlugin


class DummyEngine:
    def __init__(self, captured):
        self.captured = captured

    async def generate_response(self, messages):
        # record the prompts passed and return a valid JSON
        self.captured["messages"] = messages
        return '{"language_code": "fr"}'


@pytest.mark.asyncio
async def test_language_evaluator_uses_only_local_history(monkeypatch):
    """Ensure language detector prompt includes user message + interface history only.

    Global chat history must *not* be part of the prompt, even if present.
    """
    plugin = ReconLanguageEvaluatorPlugin()

    # patch engine/registry to return our DummyEngine
    import core.cortex_registry as registry_mod
    import core.config as config_mod

    captured = {}

    class DummyRegistry:
        def get_engine(self, name):
            return DummyEngine(captured)

        def load_engine(self, name):
            return DummyEngine(captured)

    monkeypatch.setattr(registry_mod, "get_cortex_registry", lambda: DummyRegistry())

    async def fake_active():
        return "dummy"

    monkeypatch.setattr(config_mod, "get_active_cortex_engine", fake_active)

    # prepare fake history caches: local will contain french greeting, global a spanish one
    import core.chat_history_cache as cache_mod

    async def fake_load(path):
        return deque([{"sender_name": "alice", "text": "bonjour"}])

    async def fake_global(limit=None):
        return deque([{"sender_name": "cesar", "text": "hola"}])

    monkeypatch.setattr(cache_mod, "load_chat_history", fake_load)
    monkeypatch.setattr(cache_mod, "load_global_chat_history", fake_global)

    # construct fake message
    msg = type("M", (), {"interface_path": "iface1", "text": "hello world"})

    contributions = await plugin.get_recon_contributions(
        message=msg, context_memory={}, text=msg.text, tags=None, keywords=None
    )

    # the plugin should still return a language hint
    assert contributions, "no contributions produced"
    assert contributions[0].get("language_code") == "fr"

    # the captured user prompt should contain the local history and not global
    assert "hello world" in captured["messages"][1]["content"]
    assert "bonjour" in captured["messages"][1]["content"]
    assert "hola" not in captured["messages"][1]["content"], (
        "global history leaked into prompt"
    )
