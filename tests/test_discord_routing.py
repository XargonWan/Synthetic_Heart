import asyncio


from interface.discord_interface import DiscordInterface


def test_send_message_extracts_channel_from_interface_path(monkeypatch):
    di = DiscordInterface(bot_token="")

    captured = {}

    async def fake_universal_send(func, channel_id, **kwargs):
        # Capture the channel_id that would be passed to the internal send
        captured["channel_id"] = channel_id
        captured["kwargs"] = kwargs
        return None

    # Patch the transport layer to intercept the call
    import core.transport_layer as tl

    monkeypatch.setattr(tl, "universal_send", fake_universal_send)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            di.send_message(
                {"interface_path": "discord_bot/777777/888888", "text": "hi"}
            )
        )
    finally:
        loop.close()

    assert "channel_id" in captured
    # We expect the final channel id extracted to be the channel component (888888)
    assert str(captured["channel_id"]) == "888888"
