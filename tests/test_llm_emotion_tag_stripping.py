import pytest

import core.transport_layer as transport_layer


@pytest.mark.asyncio
async def test_llm_to_interface_strips_emotion_tags_before_message_chain(monkeypatch):
    captured = {}

    async def fake_corrector_orchestrator(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "core.action_parser.corrector_orchestrator",
        fake_corrector_orchestrator,
    )

    async def fake_handle(bot, message, text, source, context=None, **kwargs):
        captured["text"] = text
        return None

    monkeypatch.setattr(
        "core.message_chain.handle_incoming_message",
        fake_handle,
    )

    async def fake_send(*args, **kwargs):
        pass

    await transport_layer.llm_to_interface(
        fake_send,
        None,
        text="Ciao {disgust 7.0}",
        chat_id=123,
        interface="telegram",
    )

    assert captured["text"] == "Ciao"
