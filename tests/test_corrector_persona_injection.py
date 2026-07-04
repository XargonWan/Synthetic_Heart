"""Regression test: the corrector must carry the active persona into its prompt.

The corrector sends a fresh single-message prompt with no system/history. Without
the persona block, a corrected turn loses the persona (identity + likes/dislikes)
and the model improvises off-character replies. This verifies the persona
identity and preferences are embedded in the correction prompt.
"""

import pytest

import core.transport_layer as transport_layer


class _FakePlugin:
    def __init__(self) -> None:
        self.captured_prompt: str | None = None

    async def handle_incoming_message(self, bot, message, prompt) -> str:
        self.captured_prompt = prompt
        # Return valid JSON so the corrector loop ends on the first attempt.
        return (
            '{"actions": [{"type": "message_telegram_bot", "payload": {"text": "hi"}}]}'
        )


class _FakePersonaManager:
    def get_static_identity_content(self, persona=None) -> str:
        return "PERSONA IDENTITY:\nName: TestSynth"

    def get_static_preference_content(self, persona=None) -> str:
        return "Likes: flowers\nDislikes: water"


@pytest.mark.asyncio
async def test_corrector_prompt_includes_persona(monkeypatch):
    fake_plugin = _FakePlugin()

    import core.plugin_instance as plugin_instance

    monkeypatch.setattr(
        plugin_instance, "get_plugin", lambda: fake_plugin, raising=False
    )
    monkeypatch.setattr(
        "core.persona_manager.get_persona_manager",
        lambda: _FakePersonaManager(),
    )

    result = await transport_layer.run_corrector_middleware(
        text="this is not valid json",
        bot=None,
        context={"interface": "telegram_bot", "interface_path": "telegram_bot/1"},
        chat_id=1,
    )

    # The corrector should have called the plugin and returned its corrected JSON.
    assert result is not None
    assert fake_plugin.captured_prompt is not None
    prompt = fake_plugin.captured_prompt
    # Persona identity + preferences must be embedded in the correction prompt.
    assert "TestSynth" in prompt
    assert "Likes: flowers" in prompt
    assert "Dislikes: water" in prompt
