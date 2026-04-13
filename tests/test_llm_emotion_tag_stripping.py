import pytest

import core.transport_layer as transport_layer
from plugins.emotion_manager import strip_emotion_tags


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


# --- Meta tag stripping ---


def test_strip_emotion_tags_removes_meta_autonomous():
    text = "Hello world {meta.autonomous: true}"
    assert strip_emotion_tags(text) == "Hello world"


def test_strip_emotion_tags_removes_meta_with_equals():
    text = "Something {meta.flag = value} here"
    assert strip_emotion_tags(text) == "Something here"


def test_strip_emotion_tags_removes_both_emotion_and_meta():
    text = "{arousal 10.0, devotion 10.0} A little longer? {meta.autonomous: true}"
    assert strip_emotion_tags(text) == "A little longer?"


def test_strip_emotion_tags_preserves_normal_text():
    text = "This is a normal message with {curly braces} but not tags"
    result = strip_emotion_tags(text)
    # The {curly braces} is not a valid emotion or meta tag, so it should be preserved
    assert "{curly braces}" in result


def test_strip_emotion_tags_handles_empty_string():
    assert strip_emotion_tags("") == ""


def test_strip_emotion_tags_meta_only():
    text = "{meta.autonomous: true}"
    assert strip_emotion_tags(text) == ""
