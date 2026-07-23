from collections import deque

import pytest

from plugins.recon.recon_language_evaluator import ReconLanguageEvaluatorPlugin


class FakeEngine:
    def __init__(self):
        self.calls = []

    async def generate_response(self, messages):
        # Record call
        self.calls.append(messages)
        user_content = messages[1]["content"] if len(messages) > 1 else ""
        # First call: message text -> return non-JSON / no language
        if "User message:" in user_content and "Chat history for" not in user_content:
            return "I cannot tell"
        # Second call: history-based -> return valid JSON
        if "Chat history for" in user_content:
            return '{"language_code": "it"}'
        return "{}"


@pytest.mark.asyncio
async def test_recon_language_evaluator_uses_history_fallback(monkeypatch):
    plugin = ReconLanguageEvaluatorPlugin()

    # Fake message with interface_path
    class Msg:
        interface_path = "telegram_bot/12345"

    msg = Msg()

    # Monkeypatch cortex engine lookup
    async def fake_get_active():
        return "fake"

    # create a shared fake engine instance so we can inspect the messages later
    fake_engine = FakeEngine()

    class FakeRegistry:
        def get_engine(self, key):
            return fake_engine

    monkeypatch.setattr("core.config.get_active_cortex_engine", fake_get_active)
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )

    # Monkeypatch chat history to return recent italian messages
    async def fake_load_chat_history(interface_path: str):
        dq = deque()
        dq.append({"sender_name": "Alice", "text": "Ciao, come va?"})
        dq.append({"sender_name": "Bob", "text": "Tutto bene, grazie."})
        return dq

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history", fake_load_chat_history
    )

    contribs = await plugin.get_recon_contributions(
        message=msg, context_memory=None, text="forwarded msg"
    )

    assert isinstance(contribs, list)
    assert len(contribs) == 1
    assert contribs[0]["type"] == "language_hint"
    assert contribs[0]["language_code"] == "it"

    # make sure the system prompt included the new weighting guidance
    assert fake_engine.calls, "engine was never invoked"
    sys_msg = fake_engine.calls[0][0]["content"]
    assert "weight" in sys_msg.lower()
    assert "3" in sys_msg
