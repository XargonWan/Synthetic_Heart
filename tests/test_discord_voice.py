import pytest
from types import SimpleNamespace

from interface.discord_interface import DiscordInterface
from core.core_initializer import PLUGIN_REGISTRY
from core import message_queue


class FakeAttachment:
    def __init__(self, data: bytes, mime: str, filename: str):
        self._data = data
        self.content_type = mime
        self.filename = filename

    async def read(self):
        return self._data


@pytest.mark.asyncio
async def test_discord_audio_attachment_transcribed(monkeypatch, tmp_path):
    # prepare fake discord message with audio attachment
    data_bytes = b"dummy audio"
    att = FakeAttachment(data_bytes, "audio/ogg", "foo.ogg")
    msg = SimpleNamespace(
        id=123,
        content="",
        author=SimpleNamespace(id=99, name="user"),
        guild=None,
        channel=SimpleNamespace(id=1, name="private"),
        mentions=[],
        role_mentions=[],
        attachments=[att],
        embeds=[],
        created_at=None,
    )

    interface = DiscordInterface(bot_token="")
    # stub out client user to avoid ignoring self messages
    interface.client = SimpleNamespace(user=SimpleNamespace(id=0))

    # fake plugin
    class FakeAuris:
        async def transcribe_audio(self, path, hint):
            return "hello disco"

    orig = PLUGIN_REGISTRY.get("auris_plugin")
    PLUGIN_REGISTRY["auris_plugin"] = FakeAuris()

    recorded = []

    async def fake_enqueue(
        bot, wrapped, interface_id, original_message, skip_mention_check
    ):
        recorded.append((wrapped, interface_id, original_message, skip_mention_check))

    monkeypatch.setattr(message_queue, "enqueue", fake_enqueue)

    try:
        await interface._process_message(msg)
        assert recorded, "enqueue should have been called"
        wrapped, iface, orig_msg, skip = recorded[0]
        assert wrapped.text == "hello disco"
        assert getattr(wrapped, "is_voice_input", False)
        assert iface == "discord_bot"
        assert orig_msg is msg
        assert skip is True
    finally:
        if orig is None:
            PLUGIN_REGISTRY.pop("auris_plugin", None)
        else:
            PLUGIN_REGISTRY["auris_plugin"] = orig


@pytest.mark.asyncio
async def test_discord_audio_attachment_no_auris(monkeypatch):
    # message with audio attachment but auris missing -> generic enqueue
    data_bytes = b"dummy audio"
    att = FakeAttachment(data_bytes, "audio/ogg", "foo.ogg")
    msg = SimpleNamespace(
        id=124,
        content="",
        author=SimpleNamespace(id=99, name="user"),
        guild=None,
        channel=SimpleNamespace(id=1, name="private"),
        mentions=[],
        role_mentions=[],
        attachments=[att],
        embeds=[],
        created_at=None,
    )

    interface = DiscordInterface(bot_token="")
    interface.client = SimpleNamespace(user=SimpleNamespace(id=0))

    # ensure no auris plugin
    orig = PLUGIN_REGISTRY.get("auris_plugin")
    if "auris_plugin" in PLUGIN_REGISTRY:
        del PLUGIN_REGISTRY["auris_plugin"]

    recorded = []

    async def fake_enqueue(
        bot, wrapped, interface_id, original_message, skip_mention_check
    ):
        recorded.append((wrapped, interface_id, original_message, skip_mention_check))

    monkeypatch.setattr(message_queue, "enqueue", fake_enqueue)

    try:
        await interface._process_message(msg)
        assert recorded, "message should still be enqueued"
        wrapped, iface, orig_msg, skip = recorded[0]
        # in this case text remains empty and attachments still present
        assert wrapped.text == ""
        assert wrapped.attachments
        assert getattr(wrapped.attachments[0], "content_type", None) == "audio/ogg"
        assert iface == "discord_bot"
    finally:
        if orig is None:
            PLUGIN_REGISTRY.pop("auris_plugin", None)
        else:
            PLUGIN_REGISTRY["auris_plugin"] = orig
