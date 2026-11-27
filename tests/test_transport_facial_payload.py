import pytest


@pytest.mark.asyncio
async def test_universal_send_cleans_facial_tags_in_message_payload(monkeypatch):
    import core.transport_layer as transport
    import core.action_parser as action_parser

    captured = {}

    async def fake_run_actions(actions, context, bot, message):
        captured["actions"] = actions
        return {"processed": actions, "failed_actions": []}

    monkeypatch.setattr(action_parser, "run_actions", fake_run_actions)

    from core.animation_handler import get_karada_state_server

    class DummyKarada:
        def apply_expression_tags(self, text: str, audio_duration_s=None):
            assert "[em_grin:0.8]" in text
            return "Ciao come va?"

    monkeypatch.setattr(
        "core.animation_handler.get_karada_state_server", lambda: DummyKarada()
    )
    assert get_karada_state_server is not None

    class DummyBot:
        def get_interface_id(self):
            return "telegram_bot"

        async def send_message(self, *args, text=None, **kwargs):
            captured["sent_text"] = text
            return None

    try:
        payload = (
            '{"actions":[{"type":"message_telegram_bot",'
            '"payload":{"text":"Ciao [em_grin:0.8] come va?"}}]}'
        )
        bot = DummyBot()
        await transport.universal_send(
            bot.send_message,
            123,
            text=payload,
            chat_id=123,
            interface_path="telegram_bot/123",
        )
    finally:
        pass

    assert "actions" in captured
    assert captured["actions"][0]["payload"]["text"] == "Ciao come va?"
