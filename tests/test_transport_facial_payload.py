import pytest


@pytest.mark.asyncio
async def test_universal_send_cleans_facial_tags_in_message_payload(monkeypatch):
    import core.transport_layer as transport
    import core.core_initializer as ci
    import core.action_parser as action_parser

    captured = {}

    async def fake_run_actions(actions, context, bot, message):
        captured["actions"] = actions
        return {"processed": actions, "failed_actions": []}

    monkeypatch.setattr(action_parser, "run_actions", fake_run_actions)

    class DummyFacialPlugin:
        async def process_message_text(
            self, text: str, session_id: str, audio_duration_s=None
        ):
            assert session_id == "telegram_bot/123"
            return "Ciao come va?"

    class DummyBot:
        def get_interface_id(self):
            return "telegram_bot"

        async def send_message(self, *args, text=None, **kwargs):
            captured["sent_text"] = text
            return None

    plugin_backup = dict(ci.PLUGIN_REGISTRY)
    ci.PLUGIN_REGISTRY["facial_plugin_test"] = DummyFacialPlugin()

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
        ci.PLUGIN_REGISTRY.clear()
        ci.PLUGIN_REGISTRY.update(plugin_backup)

    assert "actions" in captured
    assert captured["actions"][0]["payload"]["text"] == "Ciao come va?"
